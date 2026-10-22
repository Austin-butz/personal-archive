"""Reading Apple apps' private SQLite stores (Reminders now, Notes later).

The live database is never queried in place: SQLite's backup API copies it into
memory, which gives a consistent snapshot even while the app is writing (the stores
use WAL). The live file is opened read-only for the copy.

These schemas are undocumented Core Data and can change with macOS updates. Queries
name their columns explicitly, so a renamed or removed column makes the fetch fail
loudly instead of storing wrong data.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def snapshot(path: Path) -> sqlite3.Connection:
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        dst = sqlite3.connect(":memory:")
        src.backup(dst)
    except sqlite3.OperationalError as e:
        raise PermissionError(
            f"can't read {path} ({e}). The process running ingest needs Full Disk Access: "
            "System Settings > Privacy & Security > Full Disk Access") from e
    src.close()
    dst.row_factory = sqlite3.Row
    return dst


def core_data_time(seconds) -> datetime | None:
    """Core Data timestamps are seconds since 2001-01-01 UTC."""
    return None if seconds is None else CORE_DATA_EPOCH + timedelta(seconds=seconds)
