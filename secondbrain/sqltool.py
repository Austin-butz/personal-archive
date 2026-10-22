"""`sql`: run one read-only SELECT against the archive, for an AI (or a human) to poke at.

The connection is opened read-only *and* fenced with an authorizer that only allows
reads, so neither a model nor a typo can write. Output is a compact text table with a
row cap, because the reader has a context window, not a screen.
"""
import json
import sqlite3

import sqlite_vec

MAX_CELL = 300
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE}


def _authorize(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def connect_readonly(path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA query_only = 1")
    db.set_authorizer(_authorize)
    return db


def query(db: sqlite3.Connection, sql: str, limit: int = 200) -> tuple[list[str], list[tuple], bool]:
    """Returns (columns, rows, truncated). Raises sqlite3.Error for bad or non-read SQL."""
    sql = sql.strip().rstrip(";")
    if not sql:
        raise sqlite3.Error("empty query")
    cur = db.execute(sql)  # a second statement makes sqlite3 raise "one statement at a time"
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(limit + 1)
    return cols, [tuple(r) for r in rows[:limit]], len(rows) > limit


def _cell(v) -> str:
    if v is None:
        return "NULL"
    s = str(v)
    s = " ".join(s.split())
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "…"


def render(cols, rows, truncated, limit) -> str:
    if not cols:
        return "(no result)"
    table = [cols] + [[_cell(v) for v in r] for r in rows]
    widths = [min(max(len(r[i]) for r in table), MAX_CELL) for i in range(len(cols))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    lines.append(f"({len(rows)} rows{f', truncated at {limit}' if truncated else ''})")
    return "\n".join(lines)


def render_json(cols, rows, truncated) -> str:
    return json.dumps({"columns": cols, "rows": rows, "truncated": truncated},
                      ensure_ascii=False, default=str)


def run(db_path, sql: str, limit: int = 200, as_json: bool = False) -> str:
    db = connect_readonly(db_path)
    try:
        cols, rows, truncated = query(db, sql, limit)
    finally:
        db.close()
    return render_json(cols, rows, truncated) if as_json else render(cols, rows, truncated, limit)
