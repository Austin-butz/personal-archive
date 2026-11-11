# personal-archive

A simpler take on my previous personal knowledge base projects. Removes all the complex graph walking and entity extraction in favor of a single vector DB, with more focus on consistent code, contracts for the source fetchers, and usability. Fetches from Obsidian notes, Apple Calendar, Apple Reminders and Google Health. AI agents get a read-only `sql` command which is significantly more effective than my previous projects' algorithms. At single-user scale semantic search already finds the right chunk, so this one keeps what matters for an archive: append-never-overwrite, content-hash identity, stale-don't-delete, and a safety check that refuses to mark rows stale when a source comes back suspiciously empty. See DESIGN.md for the stop-sign list and IDEAS.md for pending ideas.

## Setup (Mac mini)

```sh
brew install uv ollama
ollama serve &                 # or: brew services start ollama
ollama pull nomic-embed-text   # 768-d local embeddings
uv sync                        # creates .venv with sqlite-vec + pytest
uv run pytest -q               # no Ollama or calendar access needed
./tools/calendar-export/build.sh   # needs Xcode; builds build/CalendarExport.app
```

The first calendar ingest shows a "CalendarExport would like full access to your
calendars" prompt; click Allow. Rebuilding the helper means approving it again.

Use uv's (or Homebrew's) Python, not `/usr/bin/python3` — Apple's build can't load
SQLite extensions, which sqlite-vec needs.

## Use

```sh
export OBSIDIAN_VAULT="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Main"
uv run python -m secondbrain ingest obsidian
uv run python -m secondbrain ingest calendar
uv run python -m secondbrain ingest reminders
uv run python -m secondbrain auth google       # once; opens a browser
uv run python -m secondbrain ingest health
uv run python -m secondbrain search "what did I plant last spring" --since 2025-03-01
uv run python -m secondbrain status
uv run python -m secondbrain recent --hours 24      # what changed, for an AI reader
uv run python -m secondbrain sql "SELECT metric, count(*) FROM health_metrics GROUP BY 1"
```

### Logging meals and water to Google Health

`log` is the one command that writes to Google. It pushes "anonymous" entries: the food
name and numbers you give, with no food-database lookup.

```sh
uv run python -m secondbrain log meal "chicken and rice" --kcal 650 --protein 45 --meal dinner
uv run python -m secondbrain log water 16oz --at 14:30      # oz, ml, l, cup(s)
uv run python -m secondbrain log list                       # what was pushed from here
uv run python -m secondbrain log undo [ID]                  # delete from Google; default latest
```

Each command prints the entry and asks before pushing; `--yes` skips the question (and is
required when stdin isn't a terminal), `--dry-run` prints the request body instead.
`--at` takes `HH:MM` for today or an ISO date-time, in local time; the default is now.
Google can't edit an anonymous meal after creation, so fix a mistake with `undo` and log
it again. Pushes are recorded in the `health_writes` table.

Writing needs the `googlehealth.nutrition.writeonly` scope. Tokens from before it was
added still work for ingest; `log` asks you to re-run `auth google` once.

Over SSH, where the browser can't reach this machine: `auth google --headless` prints the
consent URL; approve it on any device, then pass the `http://localhost:8765/?...` address
the browser lands on (the page won't load) to `auth google --finish 'URL'` within a few minutes.

Ingest exits with code 2 if the safety check trips (a source looks unexpectedly empty).
If the drop is real, re-run with `--force`.

Google Health needs a Desktop OAuth client JSON at
`~/.config/secondbrain/google_client_secret.json` (outside the repo). The refresh token is
stored next to it. `ingest health` runs each metric separately and exits 1 if any metric
failed, 2 if any safety check tripped.

The database is `./secondbrain.db` (override with `--db` or `SECONDBRAIN_DB`).
