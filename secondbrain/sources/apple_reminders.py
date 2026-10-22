"""Apple Reminders -> reminders, read from the Reminders app's own database (DESIGN.md §6).

EventKit has no notion of subtasks, and a large share of reminders are subtasks, so
this reads the Core Data store directly to keep each subtask's parent.
"""
import os
from datetime import date, datetime
from pathlib import Path

from ..apple_store import core_data_time, snapshot
from ..chunking import chunk_text
from ..hashing import utc_iso

STORES = Path(os.environ.get(
    "REMINDERS_STORES",
    "~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores")).expanduser()

PRIORITY = {1: "high", 5: "medium", 9: "low"}

QUERY = """
SELECT r.ZCKIDENTIFIER   AS external_id,
       r.ZTITLE          AS title,
       r.ZNOTES          AS notes,
       r.ZDUEDATE        AS due,
       r.ZALLDAY         AS all_day,
       r.ZCOMPLETED      AS completed,
       r.ZCOMPLETIONDATE AS completed_date,
       r.ZPRIORITY       AS priority,
       r.ZCREATIONDATE   AS created,
       l.ZNAME           AS list_name,
       p.ZCKIDENTIFIER   AS parent_external_id,
       p.ZTITLE          AS parent_title
FROM ZREMCDREMINDER r
LEFT JOIN ZREMCDBASELIST l ON l.Z_PK = r.ZLIST
LEFT JOIN ZREMCDREMINDER p ON p.Z_PK = r.ZPARENTREMINDER
WHERE r.ZMARKEDFORDELETION = 0
  AND COALESCE(l.ZMARKEDFORDELETION, 0) = 0
  AND COALESCE(p.ZMARKEDFORDELETION, 0) = 0
"""


def _when(seconds, all_day: bool) -> str | None:
    t = core_data_time(seconds)
    if t is None:
        return None
    # All-day due dates are stored at UTC midnight with no time zone.
    return t.date().isoformat() if all_day else utc_iso(t)


class AppleReminders:
    source = "apple_reminders"
    table = "reminders"
    columns = ["source", "external_id", "list_name", "title", "notes", "due_at", "completed_at",
               "priority", "created_at", "parent_external_id", "parent_title", "occurred_at"]

    def __init__(self, stores: Path = STORES):
        self.stores = Path(stores)

    def fetch(self):
        # glob() swallows "Operation not permitted", which is how macOS privacy controls
        # refuse a process without Full Disk Access; list the folder first to surface it.
        try:
            os.listdir(self.stores)
        except PermissionError as e:
            raise PermissionError(
                f"can't list {self.stores} ({e}). The process running ingest needs Full Disk "
                "Access: System Settings > Privacy & Security > Full Disk Access. Under "
                "launchd, grant it to /bin/bash.") from e
        files = sorted(self.stores.glob("Data-*.sqlite"))
        if not files:
            raise FileNotFoundError(f"no Reminders stores in {self.stores}")
        items = []
        for f in files:
            db = snapshot(f)
            try:
                rows = db.execute(QUERY).fetchall()
            finally:
                db.close()
            for r in rows:
                if r["title"] is None:
                    raise RuntimeError(
                        f"a reminder in {f.name} has no title; Apple may have moved titles "
                        "out of ZTITLE. Refusing to ingest rather than store blank reminders.")
                items.append(self._item(r))
        return items, ("source = ?", (self.source,))

    def _item(self, r) -> dict:
        due = _when(r["due"], bool(r["all_day"]))
        completed = _when(r["completed_date"], False) if r["completed"] else None
        created = _when(r["created"], False)
        occurred = completed or due or created
        return {
            "source": self.source,
            "external_id": r["external_id"],
            "list_name": r["list_name"],
            "title": r["title"],
            "notes": r["notes"] or None,
            "due_at": due,
            "completed_at": completed,
            "priority": r["priority"] or 0,
            "created_at": created,
            "parent_external_id": r["parent_external_id"],
            "parent_title": r["parent_title"],
            "occurred_at": f"{occurred}T00:00:00Z" if occurred and len(occurred) == 10 else occurred,
        }

    def hashed_fields(self, item):
        return {k: item[k] for k in ("external_id", "list_name", "title", "notes", "due_at",
                                     "completed_at", "priority", "parent_external_id",
                                     "parent_title")}

    def chunk_texts(self, row):
        status = f"done {_day(row['completed_at'])}" if row["completed_at"] else "open"
        meta = [f"List: {row['list_name']}"]
        if row["parent_title"]:
            meta.append(f"Subtask of: {row['parent_title']}")
        if row["due_at"]:
            meta.append(f"Due: {_day(row['due_at'], with_time=len(row['due_at']) > 10)}")
        if row["priority"] in PRIORITY:
            meta.append(f"Priority: {PRIORITY[row['priority']]}")
        body = " · ".join(meta) + (f"\n\n{row['notes']}" if row["notes"] else "")
        return chunk_text(body, f"Reminder: {row['title']} ({status})")


def _day(value: str, with_time: bool = False) -> str:
    if len(value) == 10:
        return f"{date.fromisoformat(value):%a %Y-%m-%d}"
    t = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    return f"{t:%a %Y-%m-%d %H:%M}" if with_time else f"{t:%a %Y-%m-%d}"
