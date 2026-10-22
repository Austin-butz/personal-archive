"""Semantic search with an optional time filter (DESIGN.md §7)."""
import sqlite_vec

K_MAX = 4096  # sqlite-vec's cap on k


def search(db, embedder, query, limit=10, since=None, until=None, include_stale=False):
    """Nearest chunks to `query`. Stale chunks (deleted or superseded at the source) are
    left out unless `include_stale`; they are history, not current state."""
    q = sqlite_vec.serialize_float32(embedder.query(query))
    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    k = min(limit * 5, K_MAX)
    while True:
        rows = db.execute(
            """SELECT c.*, v.distance FROM
                 (SELECT rowid, distance FROM chunks_vec
                  WHERE embedding MATCH ? AND k = ?) v
               JOIN chunks c ON c.id = v.rowid
               ORDER BY v.distance""",
            (q, k)).fetchall()
        hits = [r for r in rows
                if (include_stale or r["stale_at"] is None)
                and (since is None or r["occurred_at"] >= since)
                and (until is None or r["occurred_at"] < until)]
        if len(hits) >= limit or k >= total or k >= K_MAX:
            return hits[:limit]
        k = min(k * 4, K_MAX)
