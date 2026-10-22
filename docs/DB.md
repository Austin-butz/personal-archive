# secondbrain database guide

One SQLite file (`secondbrain.db`) holding a personal archive: notes, calendar events,
reminders and health metrics, plus text chunks with embeddings for semantic search.
This guide is for anyone, human or AI, writing SQL against it. Full design: `DESIGN.md`.

## Rules that apply to every source table

- **Append-only.** Content columns are never updated. When an item changes at its
  source, a *new row* is inserted and the old row is marked stale. So the same reminder
  or event can appear several times; only one version is current.
- **Current rows have `stale_at IS NULL`.** Almost every query wants
  `WHERE stale_at IS NULL`. Rows with `stale_at` set were deleted at the source or
  superseded by a newer version (the newer version has the same `external_id`).
- **Timestamps are text.** `occurred_at`, `first_seen_at`, `last_seen_at`, `stale_at`
  are UTC ISO-8601 strings (`2026-10-20T13:05:00Z`; bookkeeping ones have
  microseconds). They sort and compare correctly as strings. Date-only values
  (`2026-10-20`) mean an all-day or whole-day item, in the person's local time zone.
- `occurred_at` is the time the item is *about* (event start, reminder completion or
  due date, the day a health metric covers). `first_seen_at` is when ingest first
  stored this row, which is also when it was created or last changed at the source
  (as far as the archive knows). `last_seen_at` is just the last ingest run that saw
  it; it is not an edit time.
- `external_id` is the source's own identifier; `content_hash` is the row's identity.

Handy SQLite: `date('now')`, `date('now', '-7 days')`, `datetime('now')` (UTC),
`substr(occurred_at, 1, 10)` for the day, `json_extract(details, '$.key')` for JSON.

## Tables

### health_metrics  (source = 'google_health')

One row per metric per day, or per sleep session, exercise, weigh-in or body-fat reading.

| column | meaning |
|---|---|
| `metric` | which metric, see list below |
| `value`, `unit` | the number and its unit |
| `start_at` | `YYYY-MM-DD` for daily metrics; UTC ISO for sleep, exercise, weight, body fat |
| `end_at` | UTC ISO end for sleep and exercise, else NULL |
| `details` | JSON with extra fields (see below) or NULL |
| `occurred_at` | = `start_at` (daily metrics at `T00:00:00Z`) |

Daily metrics (`start_at` is a date; totals cover the whole day, so **today's row does
not exist until tomorrow**):

| metric | unit | value | details JSON |
|---|---|---|---|
| `steps` | steps | daily total | |
| `distance` | km | daily total | |
| `floors` | floors | daily total | |
| `calories` | kcal | total burned (incl. resting) | |
| `active_calories` | kcal | burned by activity | |
| `active_zone_minutes` | min | fat burn + cardio + peak | `fat_burn`, `cardio`, `peak` |
| `active_minutes` | min | total across levels | minutes per activity level, e.g. `fairly_active`, `very_active` |
| `hr_zones` | min | minutes at moderate intensity or above | minutes per zone: `light`, `moderate`, `vigorous`, `peak` |
| `sedentary` | min | sedentary time | |
| `heart_rate` | bpm | daily average | `min_bpm`, `max_bpm` |
| `resting_hr` | bpm | resting heart rate | |
| `hrv` | ms | average nightly HRV (RMSSD) | `deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds` |
| `spo2` | % | average overnight SpO2 | `lowerBoundPercentage`, `upperBoundPercentage` |
| `respiratory_rate` | breaths/min | overnight | |
| `sleep_temperature` | °C | nightly skin temperature | `baseline_c`, `vs_baseline_c` (deviation; the useful number) |
| `vo2max` | ml/kg/min | cardio fitness estimate | `fitness_level`, `estimated` |

Sessions and samples (`start_at` is a UTC timestamp):

| metric | unit | value | details JSON |
|---|---|---|---|
| `sleep` | min | minutes asleep; `start_at`/`end_at` = in bed. **Dated by the night it ends** | `minutes_awake`, `minutes_in_bed`, `stages` {`light`,`deep`,`rem`,`awake`}, `main_sleep` (false = nap) |
| `exercise` | min | active duration of one workout | `type` (e.g. Walk, Run, Weights), `distance_km`, `calories_kcal`, `avg_hr_bpm`, `steps`, `active_zone_minutes`, `notes` |
| `weight` | kg | one weigh-in | `notes` |
| `body_fat` | % | one reading | |

Examples:

```sql
-- last 7 days of a daily metric
SELECT start_at, value FROM health_metrics
WHERE metric = 'resting_hr' AND stale_at IS NULL AND start_at >= date('now', '-7 days')
ORDER BY start_at;

-- 30-day average for a baseline
SELECT round(avg(value), 1) FROM health_metrics
WHERE metric = 'sleep' AND stale_at IS NULL AND json_extract(details, '$.main_sleep') = 1
  AND start_at >= datetime('now', '-30 days');

-- recent workouts
SELECT start_at, value AS minutes, json_extract(details, '$.type') AS type,
       json_extract(details, '$.avg_hr_bpm') AS avg_hr
FROM health_metrics WHERE metric = 'exercise' AND stale_at IS NULL
ORDER BY start_at DESC LIMIT 10;
```

### reminders  (source = 'apple_reminders')

One row per reminder version. Completing a reminder creates a new row with
`completed_at` set; the open version goes stale.

| column | meaning |
|---|---|
| `title`, `notes` | text |
| `list_name` | the Reminders list |
| `due_at` | UTC ISO, or `YYYY-MM-DD` for all-day; NULL if no due date |
| `completed_at` | UTC ISO when done, NULL if open |
| `priority` | 0 none, 1 high, 5 medium, 9 low |
| `created_at` | when the reminder was created |
| `parent_external_id`, `parent_title` | set when this is a subtask; the parent gives it meaning ("Set 2" under "Leg presses") |
| `occurred_at` | `completed_at`, else `due_at`, else `created_at` |

```sql
-- open and overdue
SELECT title, list_name, due_at, priority, parent_title FROM reminders
WHERE stale_at IS NULL AND completed_at IS NULL AND due_at < datetime('now')
ORDER BY due_at;

-- completed in the last day
SELECT completed_at, title, parent_title FROM reminders
WHERE stale_at IS NULL AND completed_at >= datetime('now', '-1 day') ORDER BY completed_at;
```

### calendar_events  (source = 'macos_calendar')

One row per *occurrence* of an event (repeating series are expanded).

| column | meaning |
|---|---|
| `summary` | title |
| `description`, `location` | optional |
| `calendar_name` | which calendar |
| `starts_at`, `ends_at` | UTC ISO, or `YYYY-MM-DD` when `all_day = 1` |
| `all_day` | 0/1 |
| `occurred_at` | = `starts_at` |

```sql
SELECT starts_at, ends_at, summary, calendar_name, location FROM calendar_events
WHERE stale_at IS NULL AND occurred_at >= datetime('now') AND occurred_at < datetime('now', '+2 days')
ORDER BY starts_at;
```

### notes  (source = 'obsidian')

| column | meaning |
|---|---|
| `external_id` | vault-relative path, e.g. `Projects/garden.md` |
| `title` | file name without extension |
| `body` | the Markdown (private sections already removed) |
| `occurred_at` | frontmatter `date` if present, else the file's modification time when first seen |

Bodies can be long: select `substr(body, 1, 500)` or use `semantic_search` instead.

### chunks, chunks_vec

Text chunks (`chunks.text`) with embeddings, one or more per source row
(`source_type` = table name, `source_row_id` = that row's id). Used by semantic search;
`chunks.text` is also a readable one-paragraph rendering of any row:

```sql
SELECT c.text FROM chunks c WHERE c.source_type = 'reminders' AND c.source_row_id = 123;
```

### ingest_runs

One row per ingest run: `source` (e.g. `google_health:sleep`, `apple_reminders`),
`started_at`, `finished_at`, counts, `safety_tripped`, `error`. Use it to judge how fresh
the data is:

```sql
SELECT source, max(finished_at) FROM ingest_runs WHERE error IS NULL GROUP BY source;
```

## CLI tools for AI use

- `python -m secondbrain recent --hours 24` (or `--days 7`): what changed, plus upcoming
  events and due reminders, one line per row as `table#id`.
- `python -m secondbrain sql "SELECT ..."`: one read-only statement, text table, capped rows.
- `python -m secondbrain search "..." [--since YYYY-MM-DD]`: search by meaning over notes,
  events, reminders and health entries.
