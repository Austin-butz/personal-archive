"""The v0 definition-of-done checks from DESIGN.md §8, against the Obsidian source."""
import hashlib
import os

import pytest

from secondbrain import db as dbmod
from secondbrain.embed import DIM
from secondbrain.hashing import content_hash
from secondbrain.ingest import SafetyTripped, run
from secondbrain.search import search
from secondbrain.sources.obsidian import Obsidian


class FakeEmbedder:
    """Deterministic bag-of-words vectors, so similar text lands nearby."""
    name = "fake"

    def _vec(self, text):
        v = [0.0] * DIM
        for w in text.lower().split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def documents(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


@pytest.fixture
def env(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "garden.md").write_text("Planted tomatoes and basil in the raised bed.")
    (vault / "work.md").write_text("---\ndate: 2026-01-15\n---\nQuarterly planning meeting notes.")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "ignored.md").write_text("config, should be skipped")
    db = dbmod.connect(tmp_path / "t.db")
    return vault, db, Obsidian(vault), FakeEmbedder()


def counts(db):
    return tuple(db.execute("""SELECT
        (SELECT count(*) FROM notes WHERE stale_at IS NULL),
        (SELECT count(*) FROM notes WHERE stale_at IS NOT NULL),
        (SELECT count(*) FROM chunks WHERE stale_at IS NULL),
        (SELECT count(*) FROM chunks WHERE stale_at IS NOT NULL)""").fetchone())


def test_hash_is_stable_across_unicode_and_line_endings():
    a = content_hash({"body": "café\r\nline"})
    b = content_hash({"body": "café\nline"})
    assert a == b


def test_first_ingest_and_search(env):
    vault, db, src, emb = env
    s = run(db, src, emb)
    assert s["fetched"] == 2 and s["inserted"] == 2 and s["chunks_added"] == 2
    top = search(db, emb, "tomatoes basil", limit=1)[0]
    assert "garden.md" in top["text"]
    row = db.execute("SELECT occurred_at FROM notes WHERE external_id='work.md'").fetchone()
    assert row[0] == "2026-01-15T00:00:00Z"


def test_rerun_is_a_noop(env):
    vault, db, src, emb = env
    run(db, src, emb)
    os.utime(vault / "garden.md", (0, 0))  # touching mtime must not matter
    s = run(db, src, emb)
    assert (s["inserted"], s["marked_stale"], s["chunks_added"]) == (0, 0, 0)
    assert counts(db) == (2, 0, 2, 0)


def test_edit_creates_new_row_and_stales_old(env):
    vault, db, src, emb = env
    run(db, src, emb)
    (vault / "garden.md").write_text("Harvested zucchini today.")
    s = run(db, src, emb)
    assert (s["inserted"], s["marked_stale"], s["chunks_added"]) == (1, 1, 1)
    assert counts(db) == (2, 1, 2, 1)
    assert "zucchini" in search(db, emb, "tomatoes basil zucchini", limit=1)[0]["text"]
    assert all("tomatoes" not in r["text"] for r in search(db, emb, "tomatoes", limit=5))


def test_revert_brings_old_row_back(env):
    vault, db, src, emb = env
    run(db, src, emb)
    original = (vault / "garden.md").read_text()
    (vault / "garden.md").write_text("Harvested zucchini today.")
    run(db, src, emb)
    (vault / "garden.md").write_text(original)
    s = run(db, src, emb)
    assert s["inserted"] == 0 and s["chunks_added"] == 0
    assert counts(db) == (2, 1, 2, 1)


def test_missing_vault_writes_nothing(env, tmp_path):
    vault, db, src, emb = env
    run(db, src, emb)
    with pytest.raises(FileNotFoundError):
        run(db, Obsidian(tmp_path / "unmounted"), emb)
    assert counts(db) == (2, 0, 2, 0)


def test_empty_vault_trips_safety_check(env):
    vault, db, src, emb = env
    run(db, src, emb)
    for p in vault.glob("*.md"):
        p.unlink()
    with pytest.raises(SafetyTripped):
        run(db, src, emb)
    assert counts(db) == (2, 0, 2, 0)
    s = run(db, src, emb, force=True)
    assert s["marked_stale"] == 2
    assert counts(db) == (0, 2, 0, 2)


def test_large_drop_trips_but_small_drop_does_not(env):
    vault, db, src, emb = env
    for i in range(30):
        (vault / f"n{i}.md").write_text(f"note number {i}")
    run(db, src, emb)
    (vault / "n0.md").unlink()
    assert run(db, src, emb)["marked_stale"] == 1
    for i in range(1, 25):
        (vault / f"n{i}.md").unlink()
    with pytest.raises(SafetyTripped):
        run(db, src, emb)


def test_time_filter(env):
    vault, db, src, emb = env
    run(db, src, emb)
    hits = search(db, emb, "notes", limit=5, since="2026-01-01", until="2026-02-01")
    assert [h["text"].split("\n")[0] for h in hits] == ["Note: work.md"]


def test_long_note_is_chunked(env):
    vault, db, src, emb = env
    (vault / "long.md").write_text("# A\n\n" + ("word " * 400) + "\n\n# B\n\n" + ("more " * 400))
    run(db, src, emb)
    texts = [r[0] for r in db.execute(
        "SELECT c.text FROM chunks c JOIN notes n ON n.id = c.source_row_id "
        "WHERE n.external_id = 'long.md' ORDER BY chunk_index")]
    assert len(texts) >= 2
    assert all(t.startswith("Note: long.md\n") and len(t) <= 1500 for t in texts)
