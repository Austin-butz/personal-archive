# secondbrain

Personal archive + semantic search on SQLite. Design: [DESIGN.md](DESIGN.md).
Parked ideas: [IDEAS.md](IDEAS.md).

## Setup (Mac mini)

```sh
brew install uv ollama
ollama serve &                 # or: brew services start ollama
ollama pull nomic-embed-text   # 768-d local embeddings
uv sync                        # creates .venv with sqlite-vec + pytest
uv run pytest -q               # 10 tests, no Ollama needed
```

Use uv's (or Homebrew's) Python, not `/usr/bin/python3` — Apple's build can't load
SQLite extensions, which sqlite-vec needs.

## Use

```sh
export OBSIDIAN_VAULT=~/path/to/vault
uv run python -m secondbrain ingest obsidian
uv run python -m secondbrain search "what did I plant last spring" --since 2025-03-01
uv run python -m secondbrain status
```

Ingest exits with code 2 if the safety check trips (vault looks unexpectedly empty).
If the drop is real, re-run with `--force`.

The database is `./secondbrain.db` (override with `--db` or `SECONDBRAIN_DB`).
