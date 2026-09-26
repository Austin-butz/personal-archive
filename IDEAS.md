# Ideas — not now

Parking lot for anything that crosses a v0 stop sign. Each entry: what, why, and the
trigger that would justify building it.

## Filtered HNSW / vec0 metadata columns

- **What:** Move `occurred_at` / `source_type` / `stale_at` into sqlite-vec metadata
  columns (or an HNSW index with filter support) so filtering happens inside the KNN
  instead of over-fetch + Python filter.
- **Why:** Over-fetching gets expensive when filters are very selective on a large corpus.
- **Trigger:** Corpus exceeds ~1M chunks, or time-filtered searches visibly slow down.
