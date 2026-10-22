"""`recent`: change report over a window, rendered for an AI."""
from datetime import datetime, timezone

import pytest

from secondbrain import db as dbmod
from secondbrain import recent
from secondbrain.hashing import content_hash

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def insert(db, table, first_seen, stale=None, **cols):
    cols.setdefault("source", "test")
    cols["content_hash"] = content_hash({"t": table, **{k: str(v) for k, v in cols.items()}, "n": first_seen})
    cols.update(first_seen_at=first_seen, last_seen_at=first_seen, stale_at=stale)
    keys = list(cols)
    return db.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                      [cols[k] for k in keys]).lastrowid


@pytest.fixture
def db(tmp_path):
    db = dbmod.connect(tmp_path / "t.db")
    old, new = "2026-10-01T00:00:00.000000Z", "2026-10-21T08:00:00.000000Z"
    # health: yesterday's steps ingested this morning; an old row untouched
    insert(db, "health_metrics", new, metric="steps", value=8432, unit="steps",
           start_at="2026-10-20", occurred_at="2026-10-20T00:00:00Z")
    insert(db, "health_metrics", old, metric="steps", value=5000, unit="steps",
           start_at="2026-10-01", occurred_at="2026-10-01T00:00:00Z")
    # calendar: one upcoming event (unchanged), one edited last week's event (changed, outside window)
    insert(db, "calendar_events", old, summary="Dentist", calendar_name="Home",
           starts_at="2026-10-22T14:00:00Z", ends_at="2026-10-22T15:00:00Z", occurred_at="2026-10-22T14:00:00Z")
    insert(db, "calendar_events", new, summary="Moved standup", calendar_name="Work",
           starts_at="2026-10-28T13:00:00Z", ends_at="2026-10-28T13:15:00Z", occurred_at="2026-10-28T13:00:00Z")
    # reminders: an overdue open one, one completed today (old version stale)
    insert(db, "reminders", old, title="Mortgage", list_name="Repeat", priority=1,
           due_at="2026-10-01", occurred_at="2026-10-01T00:00:00Z")
    insert(db, "reminders", old, stale=new, external_id="ck-9", title="Call plumber", list_name="Home",
           priority=0, occurred_at="2026-10-20T00:00:00Z")
    insert(db, "reminders", new, external_id="ck-9", title="Call plumber", list_name="Home", priority=0,
           completed_at="2026-10-21T09:30:00Z", occurred_at="2026-10-21T09:30:00Z")
    # a note edited today
    insert(db, "notes", new, external_id="Daily/2026-10-21.md", title="2026-10-21",
           body="# Today\n\nSlept badly. Long walk after lunch.", occurred_at="2026-10-21T07:00:00Z")
    db.commit()
    return db


def test_report_sections_and_markers(db):
    out = recent.report(db, hours=24, ahead=48, now=NOW)
    assert "## Health metrics from Tue 2026-10-20 on" in out
    assert "+ health_metrics#1  Health: Steps, Tue 2026-10-20 | Steps: 8,432 steps" in out
    assert "  calendar_events#1  Event: Dentist" in out            # upcoming, unchanged
    assert "+ calendar_events#2  Event: Moved standup" in out      # changed, outside the window
    assert "  reminders#1  Reminder: Mortgage (open)" in out       # overdue stays listed
    assert "- reminders#2  Reminder: Call plumber (open)" in out   # superseded version
    assert "+ reminders#3  Reminder: Call plumber (done Wed 2026-10-21)" in out
    assert "+ notes#1  Note: Daily/2026-10-21.md" in out and "Slept badly. Long walk" in out
    assert "health_metrics#2" not in out                           # old, unchanged, not scheduled


def test_each_row_listed_once(db):
    out = recent.report(db, hours=24, ahead=48, now=NOW)
    assert out.count("reminders#3 ") == 1 and out.count("calendar_events#1 ") == 1


def test_week_window_includes_more(db):
    out = recent.report(db, hours=24 * 30, ahead=0, now=NOW)
    assert "+ health_metrics#2" in out and "(2)" in out.split("## Health")[1].split("\n")[0]


def test_backfilled_health_rows_are_counted_not_listed(db):
    insert(db, "health_metrics", "2026-10-21T08:00:00.000000Z", metric="steps", value=1, unit="steps",
           start_at="2026-09-01", occurred_at="2026-09-01T00:00:00Z")
    out = recent.report(db, hours=24, now=NOW)
    assert "health_metrics#3" not in out and "(1 older health rows were added or revised" in out


def test_freshness_line(db):
    db.execute("INSERT INTO ingest_runs (source, started_at, finished_at) VALUES "
               "('google_health:steps', '2026-10-21T08:00:00Z', '2026-10-21T08:01:00.5Z'),"
               "('google_health:sleep', '2026-10-21T08:00:00Z', '2026-10-21T08:02:00.5Z')")
    assert "google_health: last ingest" in recent.report(db, now=NOW)
    assert recent.freshness(db)[0].startswith("google_health: last ingest 2026-10-21")


def test_empty_db_renders(tmp_path):
    out = recent.report(dbmod.connect(tmp_path / "e.db"), now=NOW)
    assert "(none)" in out and "no ingest runs yet" in out


def test_change_sections_keep_rows_closest_to_now(db):
    new = "2026-10-21T08:00:00.000000Z"
    for i in range(recent.MAX_CHANGED + 5):
        year = 2000 + i  # 2000..2034: the ones nearest 2026 should survive
        insert(db, "calendar_events", new, summary=f"Edit {year}", calendar_name="Old",
               starts_at=f"{year}-06-01T12:00:00Z", ends_at=f"{year}-06-01T13:00:00Z",
               occurred_at=f"{year}-06-01T12:00:00Z")
    out = recent.report(db, hours=24, ahead=0, now=NOW)
    section = out.split("## Calendar: events edited")[1].split("\n## ")[0]
    assert f"(showing the {recent.MAX_CHANGED} closest to now of {recent.MAX_CHANGED + 6}" in section
    assert "Edit 2026" in section and "Edit 2000" not in section
    years = [int(l.split("Edit ")[1][:4]) for l in section.splitlines() if "Edit " in l]
    assert years == sorted(years)


def test_skip_set_prevents_repeats(db):
    shown = set()
    first = recent.report(db, hours=24, ahead=48, now=NOW, skip=shown)
    second = recent.report(db, hours=24 * 7, ahead=0, now=NOW, skip=shown)
    assert "reminders#3 " in first and "reminders#3 " not in second
    assert "rows already listed above are not repeated" in second


def test_deleted_list_collapses_to_one_line(db):
    gone, old = "2026-10-21T08:00:00.000000Z", "2026-09-01T00:00:00.000000Z"
    for i in range(4):
        insert(db, "reminders", old, stale=gone, external_id=f"w{i}", title=f"Set {i}",
               list_name="Workout (9/1)", priority=0, occurred_at="2026-09-01T00:00:00Z")
    out = recent.report(db, hours=24, now=NOW)
    assert "- reminders list deleted from the app: 'Workout (9/1)' (4 items;" in out
    assert "Set 2" not in out
    assert "- reminders#2  Reminder: Call plumber (open)" in out  # superseded rows still listed
