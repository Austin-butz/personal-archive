"""`recent`: what changed in the archive over the last N hours, rendered for an AI reader.

"Changed" follows the append-only model (DESIGN.md §2): a row is new when its
first_seen_at falls in the window (a new item, or a new version of an edited one) and
gone when its stale_at does (deleted, or superseded by a new version). On top of that,
calendar events and reminders that fall *inside* the window by their own date are
listed as "scheduled", whether or not they changed, since a daily review needs them.

Health is the exception: an ingest run backfills and revises weeks of rows at once, so
"changed in the last 24 hours" would be a dump of the whole sync. Health rows are
selected by the day they are about (from the start of the window's local day), and
older revisions are only counted.

Output is plain text: one line per row, prefixed with a change marker and the row's
table#id so an AI can follow up with `sql`.
"""
from datetime import datetime, timedelta, timezone

from .sources.apple_reminders import AppleReminders
from .sources.google_health import METRICS, _render
from .sources.macos_calendar import MacCalendar

_HEALTH = {m.name: m for m in METRICS}
MAX_ROWS = {"health_metrics": 400}
MAX_SCHEDULED = 60
MAX_CHANGED = 30  # per change section; a catch-up ingest can touch hundreds of rows
NOTE_PREVIEW = 240


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(ts: str) -> str:
    if len(ts) == 10:
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def changed(db, table: str, since: datetime, until: datetime) -> list:
    a, b = _stamp(since), _stamp(until)
    return db.execute(
        f"""SELECT * FROM {table}
            WHERE (first_seen_at >= ? AND first_seen_at < ?) OR (stale_at >= ? AND stale_at < ?)
            ORDER BY occurred_at, id""", (a, b, a, b)).fetchall()


def scheduled(db, table: str, start: datetime, end: datetime) -> list:
    """Current rows whose own date is in [start, end). Reminders also include anything
    open and overdue, since those are still "due". Health rows count from the start of
    the local day, since daily metrics are dated by day."""
    cap = MAX_ROWS.get(table, MAX_SCHEDULED)
    a, b = _stamp(start), _stamp(end)
    if table == "health_metrics":
        # Daily rows sit at T00:00:00Z of their date, so bound by the local date string.
        a = start.astimezone().date().isoformat()
    if table == "reminders":
        sql = """SELECT * FROM reminders WHERE stale_at IS NULL AND completed_at IS NULL
                 AND due_at IS NOT NULL AND occurred_at < ? ORDER BY occurred_at, id LIMIT ?"""
        return db.execute(sql, (b, cap + 1)).fetchall()
    return db.execute(
        f"""SELECT * FROM {table} WHERE stale_at IS NULL AND occurred_at >= ? AND occurred_at < ?
            ORDER BY occurred_at, id LIMIT ?""", (a, b, cap + 1)).fetchall()


# ---------------------------------------------------------------- rendering

def _one_line(text: str, limit: int | None = None) -> str:
    text = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    return text if limit is None or len(text) <= limit else text[:limit].rstrip() + "…"


def render_row(table: str, row) -> str:
    if table == "health_metrics":
        m = _HEALTH.get(row["metric"])
        body = _render(m, row) if m else f"Health: {row['metric']} {row['value']} {row['unit']}"
        return _one_line(body)
    if table == "calendar_events":
        return _one_line(MacCalendar.chunk_texts(None, row)[0], 300)
    if table == "reminders":
        return _one_line(AppleReminders.chunk_texts(None, row)[0], 300)
    if table == "notes":
        preview = " ".join(row["body"].split())
        return _one_line(f"Note: {row['external_id']} ({_local(row['occurred_at'])}) | {preview}",
                         NOTE_PREVIEW)
    return str(dict(row))


def _marker(row, since: datetime) -> str:
    if row["stale_at"] and row["stale_at"] >= _stamp(since):
        return "-"
    return "+" if row["first_seen_at"] >= _stamp(since) else " "


def _lines(table, rows, since):
    return [f"{_marker(r, since)} {table}#{r['id']}  {render_row(table, r)}" for r in rows]


def freshness(db) -> list[str]:
    rows = db.execute(
        """SELECT source, max(finished_at) AS at, error FROM ingest_runs
           WHERE finished_at IS NOT NULL GROUP BY source ORDER BY source""").fetchall()
    out = []
    for r in rows:
        src = r["source"].split(":")[0]
        if out and out[-1][0] == src:
            out[-1][1] = max(out[-1][1], r["at"])
        else:
            out.append([src, r["at"]])
    return [f"{src}: last ingest {_local(at[:19] + 'Z')}" for src, at in out]


def _collapse_deleted_lists(db, rows, since: datetime) -> tuple[list, list[str]]:
    """Reminders removed in the window whose whole list is gone from the app become one
    line per list instead of one per item; the rows stay queryable as history."""
    a = _stamp(since)
    gone = {r["list_name"] for r in db.execute(
        """SELECT list_name FROM reminders WHERE list_name IS NOT NULL
           GROUP BY list_name HAVING sum(stale_at IS NULL) = 0 AND max(stale_at) >= ?""", (a,))}
    keep, counts = [], {}
    for r in rows:
        if r["list_name"] in gone and r["stale_at"] and r["stale_at"] >= a:
            counts[r["list_name"]] = counts.get(r["list_name"], 0) + 1
        else:
            keep.append(r)
    lines = [f"- reminders list deleted from the app: {name!r} ({n} items; still in the "
             "database as history, query by list_name)" for name, n in sorted(counts.items())]
    return keep, lines


def report(db, hours: float = 24, ahead: float = 24, now: datetime | None = None,
           title: str | None = None, skip: set | None = None) -> str:
    """`skip` is a set of (table, id) already shown elsewhere; those rows are left out and
    the rows shown here are added to it, so several reports can share one prompt."""
    now = now or datetime.now(timezone.utc)
    since, until = now - timedelta(hours=hours), now
    horizon = now + timedelta(hours=ahead)
    local_now = now.astimezone()
    out = [f"# {title or f'Last {hours:g} hours'}",
           f"Now: {local_now:%a %Y-%m-%d %H:%M %Z}. Window: {since.astimezone():%Y-%m-%d %H:%M} "
           f"to {local_now:%Y-%m-%d %H:%M} local; scheduled items through "
           f"{horizon.astimezone():%Y-%m-%d %H:%M}.",
           "Markers: '+' new or changed in the window, '-' removed or superseded, ' ' unchanged. "
           "Each line is table#id; times are local unless marked Z (UTC).",
           "Data freshness: " + ("; ".join(freshness(db)) or "no ingest runs yet"), ""]

    reminders_changed, deleted_lists = _collapse_deleted_lists(
        db, changed(db, "reminders", since, until), since)
    health = scheduled(db, "health_metrics", since, horizon)
    health_ids = {r["id"] for r in health}
    revised = sum(1 for r in changed(db, "health_metrics", since, until) if r["id"] not in health_ids)
    sections = [
        (f"Health metrics from {since.astimezone():%a %Y-%m-%d} on (daily totals are dated by day; "
         f"today's are not in yet)", "health_metrics", health),
        ("Calendar: events in the window and upcoming", "calendar_events",
         scheduled(db, "calendar_events", since, horizon)),
        ("Calendar: events edited, added or removed", "calendar_events",
         [r for r in changed(db, "calendar_events", since, until)
          if not (since <= _dt(r["occurred_at"]) < horizon)]),
        ("Reminders: open and due (including overdue)", "reminders",
         scheduled(db, "reminders", since, horizon)),
        ("Reminders: completed, added, edited or deleted", "reminders", reminders_changed),
        ("Notes: added or edited (lower priority)", "notes", changed(db, "notes", since, until)),
    ]
    seen = skip if skip is not None else set()
    repeated = 0
    for heading, table, rows in sections:
        is_change = heading.endswith(("edited, added or removed", "edited or deleted",
                                      "(lower priority)"))
        cap = MAX_CHANGED if is_change else MAX_ROWS.get(table, MAX_SCHEDULED)
        fresh = [r for r in rows if (table, r["id"]) not in seen]
        repeated += len(rows) - len(fresh)
        total = len(fresh)
        truncated = total > cap
        if truncated and is_change:
            # Keep the changes closest to now (an edit to a 2019 event matters least),
            # then show them in date order.
            fresh = sorted(sorted(fresh, key=lambda r: abs((_dt(r["occurred_at"]) - now).total_seconds()))[:cap],
                           key=lambda r: (r["occurred_at"], r["id"]))
        rows = fresh[:cap]
        seen.update((table, r["id"]) for r in rows)
        extra = []
        if heading.startswith("Reminders: completed"):
            extra = [l for l in deleted_lists if ("list", l) not in seen]
            seen.update(("list", l) for l in extra)
        out.append(f"## {heading} ({len(rows) + len(extra)})")
        out += extra + _lines(table, rows, since) or ["(none)"]
        if truncated:
            which = "closest to now" if is_change else "earliest"
            out.append(f"(showing the {cap} {which} of {total}; use the sql tool for the rest)")
        if table == "health_metrics" and revised:
            out.append(f"({revised} older health rows were added or revised by the last sync; "
                       "not shown)")
        out.append("")
    if repeated:
        out.append(f"({repeated} rows already listed above are not repeated.)")
    return "\n".join(out).rstrip() + "\n"


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
