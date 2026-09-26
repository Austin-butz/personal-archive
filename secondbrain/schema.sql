-- Keep in sync with DESIGN.md §4.
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
