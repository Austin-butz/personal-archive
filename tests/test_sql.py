"""`sql`: one read-only statement, fenced."""
import sqlite3

import pytest

from secondbrain import db as dbmod
from secondbrain import sqltool


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "t.db"
    db = dbmod.connect(p)
    db.execute("INSERT INTO notes (source, external_id, title, body, content_hash, occurred_at, "
               "first_seen_at, last_seen_at) VALUES ('obsidian', 'a.md', 'a', ?, 'h1', "
               "'2026-10-01T00:00:00Z', 'x', 'x')", ("word " * 200,))
    db.commit()
    db.close()
    return p


def test_select_renders_table(path):
    out = sqltool.run(path, "SELECT id, title FROM notes")
    assert out.splitlines()[0].split() == ["id", "title"]
    assert out.endswith("(1 rows)")


def test_long_cells_are_cut_and_rows_capped(path):
    out = sqltool.run(path, "SELECT body FROM notes")
    assert "…" in out and len(out.splitlines()[2]) <= sqltool.MAX_CELL + 1
    cols, rows, truncated = sqltool.query(sqltool.connect_readonly(path),
                                          "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 50) SELECT x FROM c", limit=10)
    assert (len(rows), truncated) == (10, True)


@pytest.mark.parametrize("sql", ["DELETE FROM notes", "UPDATE notes SET title = 'b'",
                                 "INSERT INTO notes (source, body, content_hash, occurred_at, first_seen_at, last_seen_at) VALUES ('x','x','x','x','x','x')",
                                 "DROP TABLE notes", "PRAGMA journal_mode = DELETE",
                                 "ATTACH DATABASE ':memory:' AS m", "SELECT 1; DELETE FROM notes"])
def test_writes_are_refused(path, sql):
    with pytest.raises(sqlite3.Error):
        sqltool.run(path, sql)
    assert sqltool.run(path, "SELECT count(*) FROM notes").splitlines()[2] == "1"


def test_json_output(path):
    import json
    j = json.loads(sqltool.run(path, "SELECT id FROM notes", as_json=True))
    assert j == {"columns": ["id"], "rows": [[1]], "truncated": False}


def test_vec_table_is_queryable(path):
    assert "(0 rows)" in sqltool.run(path, "SELECT rowid FROM chunks_vec LIMIT 1")
