"""Calendar source: occurrence rows, windowed stale marking, export failure handling."""
import pytest

from secondbrain import db as dbmod
from secondbrain.ingest import SafetyTripped, run
from secondbrain.search import search
from secondbrain.sources.macos_calendar import MacCalendar

from fakes import FakeEmbedder


class FakeCalendar(MacCalendar):
    """MacCalendar with canned helper output instead of launching the app."""

    def __init__(self, rows, window_end="2030-01-01"):
        super().__init__(helper="/nonexistent")
        self.rows = rows
        self.window_end = window_end

    def _export(self):
        return [dict(r) for r in self.rows]


def occ(uid, start, title="Standup", **kw):
    all_day = len(start) == 10
    return {"uid": uid, "occurrence": start, "calendar": kw.get("calendar", "Work"),
            "title": title, "start": start, "end": kw.get("end", start), "all_day": all_day,
            **({"notes": kw["notes"]} if "notes" in kw else {}),
            **({"location": kw["location"]} if "location" in kw else {})}


WEEKLY = [occ("S1", f"2025-01-{d:02d}T14:00:00Z", end=f"2025-01-{d:02d}T14:15:00Z")
          for d in (6, 13, 20, 27)]


@pytest.fixture
def db(tmp_path):
    return dbmod.connect(tmp_path / "t.db")


def current(db):
    return db.execute("SELECT external_id, occurred_at FROM calendar_events "
                      "WHERE stale_at IS NULL ORDER BY occurred_at").fetchall()


def test_each_occurrence_is_its_own_row(db):
    s = run(db, FakeCalendar(WEEKLY), FakeEmbedder())
    assert (s["inserted"], s["chunks_added"]) == (4, 4)
    rows = current(db)
    assert [r["external_id"] for r in rows][0] == "S1/2025-01-06T14:00:00Z"
    assert [r["occurred_at"][:10] for r in rows] == ["2025-01-06", "2025-01-13",
                                                     "2025-01-20", "2025-01-27"]
    hits = search(db, FakeEmbedder(), "standup", limit=10, since="2025-01-15", until="2025-01-21")
    assert len(hits) == 1 and hits[0]["occurred_at"].startswith("2025-01-20")


def test_all_day_event(db):
    run(db, FakeCalendar([occ("B1", "2025-03-04", title="Sam's birthday")]), FakeEmbedder())
    row = db.execute("SELECT starts_at, occurred_at, all_day FROM calendar_events").fetchone()
    assert tuple(row) == ("2025-03-04", "2025-03-04T00:00:00Z", 1)
    text = db.execute("SELECT text FROM chunks").fetchone()[0]
    assert text.startswith("Event: Sam's birthday\nWhen: Tue 2025-03-04 (all day)")


def test_rerun_is_a_noop(db):
    run(db, FakeCalendar(WEEKLY), FakeEmbedder())
    s = run(db, FakeCalendar(WEEKLY), FakeEmbedder())
    assert (s["inserted"], s["marked_stale"], s["chunks_added"]) == (0, 0, 0)


def test_deleted_and_moved_occurrences(db):
    run(db, FakeCalendar(WEEKLY), FakeEmbedder())
    moved = dict(WEEKLY[2], start="2025-01-21T14:00:00Z", end="2025-01-21T14:15:00Z")
    s = run(db, FakeCalendar([WEEKLY[0], moved, WEEKLY[3]]), FakeEmbedder())
    # 01-13 deleted, 01-20 moved to 01-21: one new row, two stale.
    assert (s["inserted"], s["marked_stale"]) == (1, 2)
    assert [r["occurred_at"][:10] for r in current(db)] == ["2025-01-06", "2025-01-21",
                                                            "2025-01-27"]
    # The moved occurrence keeps its identity (original slot).
    assert "S1/2025-01-20T14:00:00Z" in [r["external_id"] for r in current(db)]


def test_rename_series_replaces_every_occurrence(db):
    run(db, FakeCalendar(WEEKLY), FakeEmbedder())
    renamed = [dict(r, title="Daily sync") for r in WEEKLY]
    s = run(db, FakeCalendar(renamed), FakeEmbedder())
    assert (s["inserted"], s["marked_stale"]) == (4, 4)


def test_rows_outside_window_are_never_staled(db):
    future = occ("F1", "2031-06-01T10:00:00Z", title="Far future")
    run(db, FakeCalendar(WEEKLY + [future], window_end="2032-01-01"), FakeEmbedder())
    # A later fetch with a nearer horizon doesn't see it, but mustn't stale it.
    s = run(db, FakeCalendar(WEEKLY, window_end="2030-01-01"), FakeEmbedder())
    assert s["marked_stale"] == 0
    assert len(current(db)) == 5


def test_empty_export_trips_safety_check(db):
    run(db, FakeCalendar(WEEKLY), FakeEmbedder())
    with pytest.raises(SafetyTripped):
        run(db, FakeCalendar([]), FakeEmbedder())
    assert len(current(db)) == 4


def test_missing_helper_writes_nothing(db, tmp_path):
    src = MacCalendar(helper=tmp_path / "missing.app")
    with pytest.raises(FileNotFoundError):
        run(db, src, FakeEmbedder())
    assert db.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 0
