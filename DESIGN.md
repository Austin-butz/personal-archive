# secondbrain — v0 design

> A simple, boring, trustworthy personal archive with semantic search.
> Everything else gets built on top of this later, or not at all.

## ⚠️ Deadline: Fitbit sunset — Sept 30, 2026

Health data comes first in *urgency*, even though Obsidian comes first in build order.
Do these now, before any code:

1. **Request a Google Takeout export of Fitbit data** (backstop; no code required).
2. **Register the Google Health API OAuth 2.0 client** (no approval gate).
   - Set the consent screen publishing status to **"In production" (unverified)**, not
     "Testing". Testing-mode refresh tokens expire after 7 days, which silently breaks
     nightly jobs.
   - Do the one-time interactive auth on the Mac mini and store the refresh token
     locally. Nightly jobs only ever refresh; they never prompt.
3. Pull the full available history once, into raw JSON files on disk, even before the
   `health_metrics` ingest exists. The ingester can read those files later.

---

## 1. Scope

**In:** four sources ingested into SQLite on the Mac mini, chunked, embedded, and
searchable by meaning with optional time filters.

**Build order:**

| # | Source | Input | Notes |
|---|--------|-------|-------|
| 1 | Obsidian | vault `.md` files | Easiest; proves the whole pipeline end to end |
| 2 | Calendar | `.ics` files | |
| 3 | Health | Google Health API | Urgent — see deadline above |
| 4 | Apple Notes & Reminders | local DBs | Hardest (protobuf bodies, WAL) |

**Stop signs — not in v0.** If you find yourself building one of these, stop and put it
in `IDEAS.md` instead:

- Summaries, rollups, digests
- Entity extraction, knowledge graphs, linking
- Reranking, hybrid/keyword search, query rewriting
- Any LLM call in the write path
- MCP server, web UI, API server
- Cloud database or cloud sync of the DB
- Filtered-HNSW / vec0 metadata columns (see `IDEAS.md`; revisit at >1M chunks)

Search is semantic only: embed the query, nearest neighbours, optional time filter.

---

## 2. Principles

1. **Append, never overwrite.** A changed object is a *new row*. Content columns are
   written once. The only columns ever updated are bookkeeping: `last_seen_at`, `stale_at`.
2. **Identity is the content hash.** No `external_id`-keyed upserts. If an object's
   meaningful content changes, it gets a new hash and a new row; the old row goes stale.
3. **Stale, don't delete.** Rows that disappear from the source are marked `stale_at`.
   Search filters to current rows (`stale_at IS NULL`). History stays queryable.
4. **Never trust an empty fetch.** A fetch that returns far fewer items than expected
   (unmounted vault, expired token, empty export) must not mark everything stale.
5. **One SQLite file, local only.** Back it up like any other file.

---

## 3. Content hashing

The hash must be **stable**: the same logical object must always produce the same hash,
across runs, machines and library versions.

```python
import hashlib, json, unicodedata

def normalize(v):
    if isinstance(v, str):
        v = v.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", v)
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [normalize(x) for x in v]
    return v  # int, float, bool, None

def content_hash(fields: dict) -> str:
    payload = json.dumps(normalize(fields), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Rules:

- **Include** every semantic field, *including* `external_id` when the source has one
  (so two identical notes at different paths are two rows).
- **Exclude** sync metadata: modification times, etags, sequence numbers, sync tokens,
  fetch timestamps, anything the source changes without the content changing.
- **Timestamps** that *are* content (event start, due date) are converted to UTC ISO-8601
  strings (`2026-09-26T14:00:00Z`) before hashing. All-day dates stay as `YYYY-MM-DD`.
- Each source defines its hashed field list explicitly in code (see §6). Changing that
  list changes every hash — treat it as a migration.

---

## 4. Schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- source tables
-- Common columns on every source table:
--   content_hash   sha256 of the semantic fields (§3); the row's identity
--   occurred_at    UTC ISO-8601; the time this item is "about" (for time filters)
--   first_seen_at  when ingest first inserted this row
--   last_seen_at   last ingest run that saw this exact content
--   stale_at       set when no longer present at the source; NULL = current
--   chunked_at     set once chunks + embeddings exist for this row

CREATE TABLE notes (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'obsidian' | 'apple_notes'
    external_id    TEXT,                   -- vault-relative path / Apple note id
    title          TEXT,
    body           TEXT NOT NULL,
    content_hash   TEXT NOT NULL UNIQUE,
    occurred_at    TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    stale_at       TEXT,
    chunked_at     TEXT
);

CREATE TABLE calendar_events (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'ics'
    external_id    TEXT,                   -- UID (+ RECURRENCE-ID if present)
    calendar_name  TEXT,
    summary        TEXT,
    description    TEXT,
    location       TEXT,
    starts_at      TEXT NOT NULL,
    ends_at        TEXT,
    all_day        INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT NOT NULL UNIQUE,
    occurred_at    TEXT NOT NULL,          -- = starts_at
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    stale_at       TEXT,
    chunked_at     TEXT
);

CREATE TABLE reminders (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'apple_reminders'
    external_id    TEXT,
    list_name      TEXT,
    title          TEXT NOT NULL,
    notes          TEXT,
    due_at         TEXT,
    completed_at   TEXT,
    priority       INTEGER,
    content_hash   TEXT NOT NULL UNIQUE,
    occurred_at    TEXT NOT NULL,          -- coalesce(due_at, completed_at, first_seen_at)
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    stale_at       TEXT,
    chunked_at     TEXT
);

CREATE TABLE health_metrics (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,          -- 'google_health' | 'fitbit_takeout'
    external_id    TEXT,
    metric         TEXT NOT NULL,          -- 'steps', 'resting_hr', 'sleep_minutes', ...
    value          REAL NOT NULL,
    unit           TEXT NOT NULL,
    start_at       TEXT NOT NULL,
    end_at         TEXT,
    content_hash   TEXT NOT NULL UNIQUE,
    occurred_at    TEXT NOT NULL,          -- = start_at
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    stale_at       TEXT,
    chunked_at     TEXT
);

-- Hot-path indexes for stale marking and "what needs chunking"
CREATE INDEX notes_scope      ON notes(source, stale_at);
CREATE INDEX calendar_scope   ON calendar_events(source, stale_at, starts_at);
CREATE INDEX reminders_scope  ON reminders(source, stale_at);
CREATE INDEX health_scope     ON health_metrics(source, stale_at, start_at);

-- ---------------------------------------------------------------- search
CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY,   -- = rowid in chunks_vec
    source_type     TEXT NOT NULL,         -- 'notes' | 'calendar_events' | 'reminders' | 'health_metrics'
    source_row_id   INTEGER NOT NULL,      -- id in that table
    chunk_index     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    content_hash    TEXT NOT NULL,         -- sha256(parent content_hash, chunk_index, text)
    occurred_at     TEXT NOT NULL,         -- denormalized from parent
    stale_at        TEXT,                  -- denormalized from parent
    embedding_model TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (source_type, content_hash)
);
CREATE INDEX chunks_parent ON chunks(source_type, source_row_id);
CREATE INDEX chunks_time   ON chunks(stale_at, occurred_at);

-- sqlite-vec; dimension must match the embedding model
CREATE VIRTUAL TABLE chunks_vec USING vec0(
    embedding float[768]
);

-- ---------------------------------------------------------------- bookkeeping
CREATE TABLE ingest_runs (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    fetched        INTEGER,
    inserted       INTEGER,
    marked_stale   INTEGER,
    chunks_added   INTEGER,
    safety_tripped INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
```

Notes on the schema:

- `source` is a column (not a table per source) because Obsidian and Apple Notes share
  `notes`. Stale marking is always scoped by `source`.
- Chunk `content_hash` includes the parent hash, so identical text in two different
  notes still produces two chunks. It is *not* the parent's hash alone, which would
  collide across a note's multiple chunks.
- `chunks.stale_at` is copied from the parent whenever the parent is marked stale, so
  search never needs to join back to source tables.
- `embedding_model` makes a model change detectable: re-embedding means new chunks for
  the new model, old ones marked stale.

---

## 5. Ingest contract

Every source implements two functions; the runner does everything else.

```python
def fetch() -> tuple[list[Item], Scope]: ...
    # Item = dict of content fields for this source's table (no bookkeeping columns)
    # Scope = which existing rows this fetch is authoritative for
    #         e.g. {"source": "obsidian"} or {"source": "ics", "starts_at >= ": "2025-01-01"}

def hashed_fields(item) -> dict: ...
    # The exact fields fed to content_hash() — explicit, per source.
```

The runner, per source, in one transaction for steps 3–5:

1. **Fetch.** Call `fetch()`. Any exception → record error in `ingest_runs`, stop. No
   writes.
2. **Safety check.** Count current rows in scope (`stale_at IS NULL`). If `fetched == 0`
   while current > 0, or `fetched < 0.5 × current` while current ≥ 20, set
   `safety_tripped = 1` and **skip step 4** (still insert new rows). Log loudly. A
   human confirms with `--force` if the drop is real.
3. **Upsert.** For each item:
   ```sql
   INSERT INTO <table> (..., content_hash, occurred_at, first_seen_at, last_seen_at)
   VALUES (..., :hash, :occurred_at, :run_started, :run_started)
   ON CONFLICT(content_hash) DO UPDATE
       SET last_seen_at = excluded.last_seen_at,
           stale_at     = NULL;   -- came back (e.g. undone delete)
   ```
   Content columns are never in the `SET` list.
4. **Mark stale.** Rows in scope not seen this run:
   ```sql
   UPDATE <table> SET stale_at = :now
    WHERE <scope> AND stale_at IS NULL AND last_seen_at < :run_started;
   UPDATE chunks SET stale_at = :now
    WHERE source_type = '<table>' AND stale_at IS NULL
      AND source_row_id IN (SELECT id FROM <table> WHERE stale_at = :now);
   ```
   Rows *outside* the scope are untouched — a calendar fetch of the last 90 days never
   stales events from 2019.
5. **Chunk + embed new rows only.** `WHERE chunked_at IS NULL AND stale_at IS NULL`.
   Chunk, embed, insert into `chunks` + `chunks_vec`, set `chunked_at`. Embedding failure
   leaves `chunked_at` NULL so the next run retries; it does not roll back step 3.

Then write counts to `ingest_runs`.

### Chunking

- Split on Markdown headings, then paragraphs; pack into chunks of ≤ ~1,500 characters.
  No overlap. A single oversized paragraph is hard-split.
- Prefix every chunk with a one-line header for context, e.g.
  `Note: Projects/secondbrain.md` or `Event: Dentist — 2026-10-02`. Deterministic
  template, no LLM.
- Calendar events, reminders and health rows are usually one chunk each, rendered from
  a fixed template.

---

## 6. Per-source specifics

**Obsidian**
- Walk the vault for `*.md`, skipping `.obsidian/` and `.trash/`.
- `external_id` = vault-relative POSIX path. `title` = filename stem.
- Hashed: `external_id`, `body`. (Renaming/moving a note = new row, old row stale.)
- `occurred_at`: frontmatter `date` if present, else file mtime *at first sighting*.
  mtime is not hashed, so touching a file does not create a new row.
- Scope: `{"source": "obsidian"}`. Unmounted/missing vault path → fetch raises.

**Calendar (.ics)**
- `external_id` = `UID` + (`RECURRENCE-ID` if present). Expand recurring events only
  within a bounded window; scope stale marking to that same window.
- Hashed: `external_id`, `calendar_name`, `summary`, `description`, `location`,
  `starts_at`, `ends_at`, `all_day`. Excluded: `DTSTAMP`, `LAST-MODIFIED`, `SEQUENCE`.

**Health (Google Health API)**
- v0 stores **daily-granularity** data as the API returns it (daily steps, resting HR,
  sleep duration, etc.). Intraday samples are out of scope — they would be millions of
  near-meaningless chunks.
- Hashed: `metric`, `value`, `unit`, `start_at`, `end_at`, `external_id`.
- Scope: by `source` and the fetched date range.
- Takeout files ingest into the same table with `source = 'fitbit_takeout'`.

**Apple Notes & Reminders** — design when we get there. Known issues: protobuf-encoded
note bodies, reading a live WAL-mode DB (copy the DB + `-wal` + `-shm` together, open
the copy read-only).

---

## 7. Search

```python
def search(query, limit=10, since=None, until=None):
    q = embed(query)
    k = limit * 5
    while True:
        rows = knn(chunks_vec, q, k)                 # brute force via sqlite-vec
        hits = [r for r in join_chunks(rows)
                if r.stale_at is None
                and (since is None or r.occurred_at >= since)
                and (until is None or r.occurred_at <  until)]
        if len(hits) >= limit or k >= total_chunks:
            return hits[:limit]
        k *= 4
```

Over-fetch and filter in Python. Good enough well past 100k chunks; revisit at ~1M
(see `IDEAS.md`).

---

## 8. Definition of done for v0

- [ ] `DESIGN.md` (this file)
- [ ] Fitbit Takeout requested; Google Health OAuth client registered (**by Sept 30**)
- [ ] Schema created; Obsidian ingests end to end; `search "..."` returns sensible hits
- [ ] Re-running ingest with no changes inserts 0 rows, stales 0 rows
- [ ] Editing a note → 1 new row, 1 stale row, new chunks, old chunks stale
- [ ] Unmounting the vault → safety check trips, nothing marked stale
- [ ] Calendar, then Health, then Apple Notes/Reminders, each passing the same three tests
- [ ] Nightly job on the Mac mini (launchd) runs all sources and logs to `ingest_runs`
