import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA = Path(__file__).with_name("schema.sql")
VIEWS = Path(__file__).with_name("views.sql")


def connect(path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks'").fetchone():
        db.executescript(SCHEMA.read_text())
    db.executescript(VIEWS.read_text())
    return db
