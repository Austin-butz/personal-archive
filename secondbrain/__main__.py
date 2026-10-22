"""CLI: python -m secondbrain {ingest,search,status,recent,sql,log,auth}"""
import argparse
import json
import logging
import os
import sys

from . import db as dbmod
from .embed import OllamaEmbedder
from .ingest import SafetyTripped, run
from .search import search
from .sources.apple_reminders import AppleReminders
from .sources.macos_calendar import MacCalendar
from .sources.obsidian import Obsidian


def main(argv=None):
    p = argparse.ArgumentParser(prog="secondbrain")
    p.add_argument("--db", default=os.environ.get("SECONDBRAIN_DB", "secondbrain.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest")
    ing.add_argument("source", choices=["obsidian", "calendar", "reminders", "health"])
    ing.add_argument("--vault", default=os.environ.get("OBSIDIAN_VAULT"))
    ing.add_argument("--force", action="store_true", help="mark stale even if the safety check trips")

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--since", help="YYYY-MM-DD (inclusive)")
    s.add_argument("--until", help="YYYY-MM-DD (exclusive)")

    sub.add_parser("status")

    rc = sub.add_parser("recent", help="what changed recently, formatted for an AI reader")
    rc.add_argument("--hours", type=float, default=24)
    rc.add_argument("--days", type=float, help="shorthand for --hours 24*N")
    rc.add_argument("--ahead", type=float, default=24, help="hours of upcoming calendar/reminders")

    sq = sub.add_parser("sql", help="run one read-only SELECT (for an AI or a quick look)")
    sq.add_argument("query")
    sq.add_argument("--limit", type=int, default=200)
    sq.add_argument("--json", action="store_true")

    lg = sub.add_parser("log", help="push a meal or water to Google Health")
    lsub = lg.add_subparsers(dest="what", required=True)
    w = lsub.add_parser("water", help="e.g. log water 16oz")
    w.add_argument("amount", help="number + unit: oz, ml, l, cup(s)")
    ml = lsub.add_parser("meal", help='e.g. log meal "chicken and rice" --kcal 650 --protein 45')
    ml.add_argument("food", help="what you ate, as it should show in Fitbit")
    ml.add_argument("--kcal", type=float, required=True)
    ml.add_argument("--protein", type=float, required=True, help="grams")
    ml.add_argument("--meal", choices=["breakfast", "lunch", "dinner", "snack"])
    for x in (w, ml):
        x.add_argument("--at", help="HH:MM today, or ISO date-time; default now (local time)")
        x.add_argument("--yes", action="store_true", help="push without asking")
        x.add_argument("--dry-run", action="store_true", help="print the request and stop")
    lsub.add_parser("list", help="entries pushed from here, newest first")
    ud = lsub.add_parser("undo", help="delete a pushed entry from Google (default: the latest)")
    ud.add_argument("id", nargs="?", type=int)
    ud.add_argument("--yes", action="store_true")

    au = sub.add_parser("auth", help="one-time interactive sign-in")
    au.add_argument("provider", choices=["google"])
    au.add_argument("--headless", action="store_true",
                    help="over SSH: print the URL instead of waiting for the browser's redirect")
    au.add_argument("--finish", metavar="URL",
                    help="the localhost URL the browser landed on after --headless")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.cmd == "auth":
        from . import google_auth as ga
        if a.headless:
            print("open this, approve, then copy the localhost URL your browser lands on "
                  "(it won't load) into `auth google --finish URL`:\n" + ga.headless_url())
            return 0
        if a.finish:
            ga.headless_finish(a.finish)
        else:
            ga.authorize()
        TOKEN_FILE = ga.TOKEN_FILE
        print(f"signed in; refresh token saved to {TOKEN_FILE}")
        return 0
    if a.cmd == "sql":
        from . import sqltool
        try:
            print(sqltool.run(a.db, a.query, a.limit, a.json))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    db = dbmod.connect(a.db)

    if a.cmd == "recent":
        from . import recent
        print(recent.report(db, hours=a.days * 24 if a.days else a.hours, ahead=a.ahead), end="")
    elif a.cmd == "log":
        return _log(db, a)
    elif a.cmd == "ingest":
        if a.source == "health":
            return _ingest_health(db, a.force)
        if a.source == "obsidian":
            if not a.vault:
                p.error("--vault or OBSIDIAN_VAULT is required")
            src = Obsidian(a.vault)
        elif a.source == "reminders":
            src = AppleReminders()
        else:
            src = MacCalendar()
        try:
            print(run(db, src, OllamaEmbedder(), force=a.force))
        except SafetyTripped as e:
            print(f"SAFETY CHECK TRIPPED: {e.args[0]}", file=sys.stderr)
            return 2
    elif a.cmd == "search":
        for r in search(db, OllamaEmbedder(), a.query, a.limit, a.since, a.until):
            first, _, rest = r["text"].partition("\n")
            print(f"{r['distance']:.3f}  {r['occurred_at'][:10]}  {first}")
            print("        " + rest[:200].replace("\n", " ") + "\n")
    elif a.cmd == "status":
        for t in ("notes", "calendar_events", "reminders", "health_metrics", "chunks"):
            cur, total = db.execute(
                f"SELECT count(*) FILTER (WHERE stale_at IS NULL), count(*) FROM {t}").fetchone()
            print(f"{t:16} {cur:>7} current {total:>7} total")
        for r in db.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 5"):
            print(dict(r))
    return 0


def _ingest_health(db, force):
    """One run per metric, so one failing or empty metric can't affect the others."""
    from .google_auth import session
    from .sources.google_health import METRICS, GoogleHealthMetric, HealthClient

    client, embedder, code = HealthClient(session()), OllamaEmbedder(), 0
    for m in METRICS:
        try:
            s = run(db, GoogleHealthMetric(m, client), embedder, force=force)
            print(f"{m.name:20} fetched {s['fetched']:>5}  new {s['inserted']:>5}  "
                  f"stale {s['marked_stale']:>4}")
        except SafetyTripped as e:
            print(f"{m.name:20} SAFETY CHECK TRIPPED, nothing staled: {e.args[0]}", file=sys.stderr)
            code = max(code, 2)
        except Exception as e:
            print(f"{m.name:20} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            code = max(code, 1)
    return code


def _log(db, a):
    """Writes to Google, so it shows the entry and asks first unless --yes."""
    from . import health_log as hl
    from .google_auth import NUTRITION_WRITE, session

    try:
        if a.what == "list":
            for e in hl.entries(db):
                gone = f"  [undone {e['deleted_at']}]" if e["deleted_at"] else ""
                print(f"#{e['id']:<4} {e['summary']}{gone}")
            return 0
        if a.what == "undo":
            row = hl.find_undoable(db, a.id)
            if not _confirm(f"delete from Google Health: #{row['id']} {row['summary']}", a.yes):
                return 1
            hl.undo(db, hl.HealthWriter(session(require=NUTRITION_WRITE)), row)
            print(f"undone #{row['id']}")
            return 0
        at = hl.parse_time(a.at)
        if a.what == "water":
            body = hl.hydration_body(*hl.parse_volume(a.amount), at)
        else:
            body = hl.meal_body(a.food, a.kcal, a.protein, a.meal, at)
        if a.dry_run:
            print(json.dumps(body, indent=2))
            return 0
        if not _confirm(f"push to Google Health: {hl.describe(body)}", a.yes):
            return 1
        r = hl.push(db, hl.HealthWriter(session(require=NUTRITION_WRITE)), body)
        note = "" if r["name"] else " (Google hasn't confirmed it yet; it can't be undone from here)"
        print(f"logged #{r['id']}: {r['summary']}{note}")
        return 0
    except (ValueError, LookupError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _confirm(what, yes):
    print(what)
    if yes:
        return True
    if not sys.stdin.isatty():
        print("not pushed: pass --yes to confirm", file=sys.stderr)
        return False
    return input("ok? [y/N] ").strip().lower() in ("y", "yes")


if __name__ == "__main__":
    sys.exit(main())
