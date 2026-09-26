# secondbrain

Personal archive + semantic search on SQLite. Design: [DESIGN.md](DESIGN.md).
Parked ideas: [IDEAS.md](IDEAS.md).

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
uv run python -m secondbrain auth google       # once; opens a browser
uv run python -m secondbrain ingest health
uv run python -m secondbrain search "what did I plant last spring" --since 2025-03-01
uv run python -m secondbrain status
```

Ingest exits with code 2 if the safety check trips (a source looks unexpectedly empty).
If the drop is real, re-run with `--force`.

Google Health needs a Desktop OAuth client JSON at
`~/.config/secondbrain/google_client_secret.json` (outside the repo). The refresh token is
stored next to it. `ingest health` runs each metric separately and exits 1 if any metric
failed, 2 if any safety check tripped.

The database is `./secondbrain.db` (override with `--db` or `SECONDBRAIN_DB`).
