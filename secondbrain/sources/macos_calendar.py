"""macOS Calendar -> calendar_events, one row per occurrence (DESIGN.md §6).

EventKit does the recurrence expansion, via the CalendarExport helper app in
tools/calendar-export. The helper is launched with `open` so macOS attributes the
Calendar permission to it rather than to whatever terminal or app started Python.
"""
import json
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..chunking import chunk_text

HELPER = Path(__file__).resolve().parents[2] / "build" / "CalendarExport.app"
WINDOW_START = "2000-01-01"
HORIZON_DAYS = 365


def _unix(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


class MacCalendar:
    source = "macos_calendar"
    table = "calendar_events"
    columns = ["source", "external_id", "calendar_name", "summary", "description", "location",
               "starts_at", "ends_at", "all_day", "occurred_at"]

    def __init__(self, helper=HELPER, window_start=WINDOW_START, horizon_days=HORIZON_DAYS):
        self.helper = Path(helper)
        self.window_start = window_start
        self.window_end = (date.today() + timedelta(days=horizon_days)).isoformat()

    def _export(self) -> list[dict]:
        """Run the helper; return one dict per occurrence. Raises if it didn't finish."""
        if not self.helper.is_dir():
            raise FileNotFoundError(f"{self.helper} not built: run tools/calendar-export/build.sh")
        with tempfile.TemporaryDirectory() as tmp:
            out, err = Path(tmp, "out.jsonl"), Path(tmp, "err.txt")
            subprocess.run(
                ["open", "-W", "-n", "-g", "--stdout", out, "--stderr", err, self.helper,
                 "--args", str(_unix(self.window_start)), str(_unix(self.window_end))],
                check=True, timeout=600)
            lines = out.read_text().splitlines() if out.exists() else []
            errors = err.read_text().strip() if err.exists() else ""
        rows = [json.loads(line) for line in lines]
        if not rows or not rows[-1].get("done"):
            raise RuntimeError(f"calendar export did not finish: {errors or 'no output'}")
        if rows[-1]["count"] != len(rows) - 1:
            raise RuntimeError(f"calendar export truncated: {len(rows) - 1} of {rows[-1]['count']}")
        return rows[:-1]

    def _item(self, r: dict) -> dict:
        start = r["start"]
        return {
            "source": self.source,
            # Series UID + the occurrence's original slot; Apple uses the same scheme.
            "external_id": f"{r['uid']}/{r['occurrence']}",
            "calendar_name": r["calendar"],
            "summary": r["title"],
            "description": r.get("notes"),
            "location": r.get("location"),
            "starts_at": start,
            "ends_at": r["end"],
            "all_day": int(r["all_day"]),
            "occurred_at": f"{start}T00:00:00Z" if r["all_day"] else start,
        }

    def fetch(self):
        items = [self._item(r) for r in self._export()]
        # Only rows inside the fetched window can be judged missing.
        scope = ("source = ? AND starts_at >= ? AND starts_at < ?",
                 (self.source, self.window_start, self.window_end))
        return items, scope

    def hashed_fields(self, item):
        return {k: item[k] for k in ("external_id", "calendar_name", "summary", "description",
                                     "location", "starts_at", "ends_at", "all_day")}

    def chunk_texts(self, row):
        if row["all_day"]:
            d = date.fromisoformat(row["starts_at"])
            when = f"{d:%a %Y-%m-%d} (all day)"
            if row["ends_at"] and row["ends_at"] != row["starts_at"]:
                when = f"{d:%a %Y-%m-%d} to {row['ends_at']} (all day)"
        else:
            s = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")).astimezone()
            e = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")).astimezone()
            when = f"{s:%a %Y-%m-%d %H:%M} to {e:%H:%M}" if s.date() == e.date() \
                else f"{s:%a %Y-%m-%d %H:%M} to {e:%a %Y-%m-%d %H:%M}"
        lines = [f"When: {when}", f"Calendar: {row['calendar_name']}"]
        if row["location"]:
            lines.append(f"Location: {row['location']}")
        if row["description"]:
            lines += ["", row["description"]]
        return chunk_text("\n".join(lines), f"Event: {row['summary']}")
