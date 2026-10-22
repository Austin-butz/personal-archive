"""Apple Reminders source, against a fake store shaped like Apple's Core Data schema."""
import sqlite3
from datetime import datetime, timezone

import pytest

from secondbrain import db as dbmod
from secondbrain.ingest import SafetyTripped, run
from secondbrain.search import search
from secondbrain.sources.apple_reminders import AppleReminders

from fakes import FakeEmbedder

EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def cd(iso):  # ISO string -> Core Data seconds
    return (datetime.fromisoformat(iso).replace(tzinfo=timezone.utc) - EPOCH).total_seconds()


class Store:
    """A Data-*.sqlite file with just the columns the source reads."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE ZREMCDBASELIST (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT,
                ZMARKEDFORDELETION INTEGER DEFAULT 0);
            CREATE TABLE ZREMCDREMINDER (Z_PK INTEGER PRIMARY KEY, ZCKIDENTIFIER TEXT,
                ZTITLE TEXT, ZNOTES TEXT, ZDUEDATE REAL, ZALLDAY INTEGER DEFAULT 0,
                ZCOMPLETED INTEGER DEFAULT 0, ZCOMPLETIONDATE REAL, ZPRIORITY INTEGER DEFAULT 0,
                ZCREATIONDATE REAL, ZLIST INTEGER, ZPARENTREMINDER INTEGER,
                ZMARKEDFORDELETION INTEGER DEFAULT 0, ZLASTMODIFIEDDATE REAL);""")
        self.db.execute("INSERT INTO ZREMCDBASELIST (Z_PK, ZNAME) VALUES (1, 'Home')")
        self.db.commit()

    def add(self, pk, title, **kw):
        cols = {"Z_PK": pk, "ZCKIDENTIFIER": f"ck-{pk}", "ZTITLE": title, "ZLIST": 1,
                "ZCREATIONDATE": cd("2026-09-01T12:00:00"), **kw}
        self.db.execute(f"INSERT INTO ZREMCDREMINDER ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})", list(cols.values()))
        self.db.commit()

    def update(self, pk, **kw):
        sets = ", ".join(f"{k} = ?" for k in kw)
        self.db.execute(f"UPDATE ZREMCDREMINDER SET {sets} WHERE Z_PK = ?", [*kw.values(), pk])
        self.db.commit()


@pytest.fixture
def env(tmp_path):
    stores = tmp_path / "Stores"
    stores.mkdir()
    store = Store(stores / "Data-ABC.sqlite")
    store.add(1, "Fix fence", ZDUEDATE=cd("2026-09-21T00:00:00"), ZALLDAY=1, ZPRIORITY=1)
    store.add(2, "Buy nails", ZPARENTREMINDER=1, ZNOTES="galvanized, 2 inch")
    store.add(3, "Call plumber", ZDUEDATE=cd("2026-09-22T14:30:00"))
    return store, dbmod.connect(tmp_path / "t.db"), AppleReminders(stores), FakeEmbedder()


def current(db):
    return {r["external_id"]: r for r in db.execute(
        "SELECT * FROM reminders WHERE stale_at IS NULL")}


def chunk(db, ext):
    return db.execute("SELECT c.text FROM chunks c JOIN reminders r ON r.id = c.source_row_id "
                      "WHERE c.source_type = 'reminders' AND r.external_id = ? "
                      "AND c.stale_at IS NULL", (ext,)).fetchone()[0]


def test_ingest_with_subtask_parent(env):
    store, db, src, emb = env
    s = run(db, src, emb)
    assert (s["inserted"], s["chunks_added"]) == (3, 3)
    sub = current(db)["ck-2"]
    assert (sub["parent_external_id"], sub["parent_title"], sub["list_name"]) == ("ck-1", "Fix fence", "Home")
    t = chunk(db, "ck-2")
    assert t.startswith("Reminder: Buy nails (open)\nList: Home · Subtask of: Fix fence")
    assert "galvanized" in t


def test_all_day_and_timed_due_dates(env):
    store, db, src, emb = env
    run(db, src, emb)
    r = current(db)
    assert (r["ck-1"]["due_at"], r["ck-1"]["occurred_at"]) == ("2026-09-21", "2026-09-21T00:00:00Z")
    assert r["ck-3"]["due_at"] == "2026-09-22T14:30:00Z"
    assert "Priority: high" in chunk(db, "ck-1")
    # no due date and not done -> falls back to creation time
    assert r["ck-2"]["occurred_at"] == "2026-09-01T12:00:00Z"


def test_completing_keeps_both_states(env):
    store, db, src, emb = env
    run(db, src, emb)
    store.update(3, ZCOMPLETED=1, ZCOMPLETIONDATE=cd("2026-09-23T09:00:00"))
    s = run(db, src, emb)
    assert (s["inserted"], s["marked_stale"]) == (1, 1)
    done = current(db)["ck-3"]
    assert (done["completed_at"], done["occurred_at"]) == ("2026-09-23T09:00:00Z", "2026-09-23T09:00:00Z")
    assert chunk(db, "ck-3").startswith("Reminder: Call plumber (done ")
    history = db.execute("SELECT count(*) FROM reminders WHERE external_id = 'ck-3'").fetchone()[0]
    assert history == 2


def test_rerun_and_sync_metadata_are_noops(env):
    store, db, src, emb = env
    run(db, src, emb)
    store.update(1, ZLASTMODIFIEDDATE=cd("2026-09-25T00:00:00"))
    s = run(db, src, emb)
    assert (s["inserted"], s["marked_stale"], s["chunks_added"]) == (0, 0, 0)


def test_deleted_reminders_go_stale(env):
    store, db, src, emb = env
    run(db, src, emb)
    store.update(3, ZMARKEDFORDELETION=1)
    s = run(db, src, emb)
    assert s["marked_stale"] == 1 and "ck-3" not in current(db)


def test_renaming_parent_updates_subtask(env):
    store, db, src, emb = env
    run(db, src, emb)
    store.update(1, ZTITLE="Repair fence")
    s = run(db, src, emb)
    assert (s["inserted"], s["marked_stale"]) == (2, 2)
    assert current(db)["ck-2"]["parent_title"] == "Repair fence"


def test_missing_title_refuses(env):
    store, db, src, emb = env
    store.add(4, None)
    with pytest.raises(RuntimeError, match="no title"):
        run(db, src, emb)
    assert db.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0


def test_schema_change_fails_loudly(env):
    store, db, src, emb = env
    store.db.execute("ALTER TABLE ZREMCDREMINDER RENAME COLUMN ZTITLE TO ZTITLE2")
    store.db.commit()
    with pytest.raises(Exception, match="ZTITLE"):
        run(db, src, emb)
    assert db.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0


def test_reads_every_store(env, tmp_path):
    store, db, src, emb = env
    other = Store(tmp_path / "Stores" / "Data-XYZ.sqlite")
    other.add(10, "Renew passport")
    s = run(db, src, emb)
    assert s["inserted"] == 4


def test_empty_fetch_trips_safety(env, tmp_path):
    store, db, src, emb = env
    run(db, src, emb)
    for pk in (1, 2, 3):
        store.update(pk, ZMARKEDFORDELETION=1)
    with pytest.raises(SafetyTripped):
        run(db, src, emb)
    assert len(current(db)) == 3


def test_search_by_due_week(env):
    store, db, src, emb = env
    run(db, src, emb)
    hits = search(db, emb, "plumber", limit=5, since="2026-09-22", until="2026-09-23")
    assert [h["text"].split("\n")[0] for h in hits] == ["Reminder: Call plumber (open)"]


def test_unreadable_store_folder_names_full_disk_access(env, tmp_path):
    store, db, src, emb = env
    src.stores.chmod(0)
    try:
        with pytest.raises(PermissionError, match="Full Disk Access"):
            run(db, src, emb)
    finally:
        src.stores.chmod(0o755)
