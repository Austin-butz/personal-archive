"""The ingest runner (DESIGN.md §5). Sources implement fetch/hashed_fields/chunk_texts."""
import logging
import sqlite3

import sqlite_vec

from .hashing import content_hash, now_stamp

log = logging.getLogger(__name__)

SAFETY_MIN_ROWS = 20
SAFETY_RATIO = 0.5


class SafetyTripped(Exception):
    pass


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def run(db: sqlite3.Connection, src, embedder, force: bool = False) -> dict:
    started = now_stamp()
    run_id = db.execute("INSERT INTO ingest_runs (source, started_at) VALUES (?, ?)",
                        (src.source, started)).lastrowid
    db.commit()
    stats = {"fetched": 0, "inserted": 0, "marked_stale": 0, "chunks_added": 0,
             "safety_tripped": 0, "error": None}

    def finish():
        db.execute(
            """UPDATE ingest_runs SET finished_at = ?, fetched = ?, inserted = ?,
                   marked_stale = ?, chunks_added = ?, safety_tripped = ?, error = ?
               WHERE id = ?""",
            (now_stamp(), stats["fetched"], stats["inserted"], stats["marked_stale"],
             stats["chunks_added"], stats["safety_tripped"], stats["error"], run_id))
        db.commit()

    t = src.table
    try:
        # 1. Fetch. Nothing is written if this raises.
        items, (scope_sql, scope_params) = src.fetch()
        stats["fetched"] = len(items)

        # 2. Safety check.
        current = _count(db, f"SELECT count(*) FROM {t} WHERE {scope_sql} AND stale_at IS NULL",
                         scope_params)
        suspicious = current > 0 and (
            len(items) == 0
            or (current >= SAFETY_MIN_ROWS and len(items) < SAFETY_RATIO * current))
        if suspicious and not force:
            stats["safety_tripped"] = 1
            log.warning("%s: fetched %d items but %d are current; NOT marking anything stale. "
                        "Re-run with --force if this drop is real.", src.source, len(items), current)

        with db:
            # 3. Upsert. Content columns are never updated.
            before = _count(db, f"SELECT count(*) FROM {t}")
            cols = src.columns + ["content_hash", "first_seen_at", "last_seen_at"]
            sql = (f"INSERT INTO {t} ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' * len(cols))}) "
                   "ON CONFLICT(content_hash) DO UPDATE "
                   "SET last_seen_at = excluded.last_seen_at, stale_at = NULL")
            for item in items:
                h = content_hash(src.hashed_fields(item))
                db.execute(sql, [item[c] for c in src.columns] + [h, started, started])
            stats["inserted"] = _count(db, f"SELECT count(*) FROM {t}") - before

            # Rows that came back bring their chunks back too.
            db.execute(
                f"""UPDATE chunks SET stale_at = NULL
                    WHERE source_type = ? AND stale_at IS NOT NULL AND embedding_model = ?
                      AND source_row_id IN (SELECT id FROM {t} WHERE last_seen_at = ?)""",
                (t, embedder.name, started))

            # 4. Mark stale — only within this fetch's scope.
            if not stats["safety_tripped"]:
                stats["marked_stale"] = db.execute(
                    f"""UPDATE {t} SET stale_at = ?
                        WHERE {scope_sql} AND stale_at IS NULL AND last_seen_at < ?""",
                    (started, *scope_params, started)).rowcount
                db.execute(
                    f"""UPDATE chunks SET stale_at = ?
                        WHERE source_type = ? AND stale_at IS NULL
                          AND source_row_id IN (SELECT id FROM {t} WHERE stale_at = ?)""",
                    (started, t, started))

        # 5. Chunk + embed new rows only. One commit per row; failures retry next run.
        rows = db.execute(f"SELECT * FROM {t} WHERE chunked_at IS NULL AND stale_at IS NULL "
                          "ORDER BY id").fetchall()
        for row in rows:
            texts = src.chunk_texts(row)
            vecs = embedder.documents(texts)
            with db:
                for i, (text, vec) in enumerate(zip(texts, vecs)):
                    h = content_hash({"parent": row["content_hash"], "i": i, "text": text})
                    cur = db.execute(
                        """INSERT INTO chunks (source_type, source_row_id, chunk_index, text,
                               content_hash, occurred_at, embedding_model, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(source_type, content_hash) DO NOTHING""",
                        (t, row["id"], i, text, h, row["occurred_at"], embedder.name, now_stamp()))
                    if cur.rowcount:
                        db.execute("INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                                   (cur.lastrowid, sqlite_vec.serialize_float32(vec)))
                        stats["chunks_added"] += 1
                db.execute(f"UPDATE {t} SET chunked_at = ? WHERE id = ?", (now_stamp(), row["id"]))
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        finish()
        raise
    finish()
    if stats["safety_tripped"]:
        raise SafetyTripped(stats)
    return stats
