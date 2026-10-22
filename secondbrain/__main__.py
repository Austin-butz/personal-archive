"""CLI: python -m secondbrain {ingest,search,status,recent,sql,auth}"""
import argparse
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

    au = sub.add_parser("auth", help="one-time interactive sign-in")
    au.add_argument("provider", choices=["google"])
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.cmd == "auth":
        from .google_auth import TOKEN_FILE, authorize
        authorize()
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


if __name__ == "__main__":
    sys.exit(main())
