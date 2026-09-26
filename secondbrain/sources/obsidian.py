"""Obsidian vault -> notes (DESIGN.md §6)."""
import re
from datetime import datetime, timezone
from pathlib import Path

from ..chunking import chunk_text
from ..hashing import utc_iso
from ..private import strip_private

SKIP_DIRS = {".obsidian", ".trash"}
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_DATE = re.compile(r"^date:\s*['\"]?(\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?)",
                   re.M)


def _frontmatter_date(text):
    fm = _FRONTMATTER.match(text)
    m = fm and _DATE.search(fm.group(1))
    if not m:
        return None
    try:
        return utc_iso(datetime.fromisoformat(m.group(1).replace("Z", "+00:00")))
    except ValueError:
        return None


class Obsidian:
    source = "obsidian"
    table = "notes"
    columns = ["source", "external_id", "title", "body", "occurred_at"]

    def __init__(self, vault):
        self.vault = Path(vault).expanduser()

    def _skipped(self, path):
        parts = path.relative_to(self.vault).parts[:-1]
        return any(p in SKIP_DIRS or p.startswith(".") for p in parts)

    def fetch(self):
        if not self.vault.is_dir():
            raise FileNotFoundError(f"vault not found (not mounted?): {self.vault}")
        # iCloud swaps offloaded files for hidden `.name.md.icloud` placeholders. Those
        # notes would silently drop out of the fetch and be marked stale, so refuse.
        evicted = sorted(p.relative_to(self.vault).as_posix()
                         for p in self.vault.rglob("*.md.icloud") if not self._skipped(p))
        if evicted:
            raise RuntimeError(f"{len(evicted)} notes are offloaded to iCloud, not on disk "
                               f"(e.g. {evicted[0]}); download them first (Finder > "
                               "Download Now, or `brctl download <vault>`)")
        items = []
        for path in sorted(self.vault.rglob("*.md")):
            if self._skipped(path):
                continue
            rel = path.relative_to(self.vault)
            # Private sections never leave this function: not stored, hashed or embedded.
            body = strip_private(path.read_text(encoding="utf-8", errors="replace"))
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            items.append({
                "source": self.source,
                "external_id": rel.as_posix(),
                "title": path.stem,
                "body": body,
                # Not hashed: only the first sighting's value is kept.
                "occurred_at": _frontmatter_date(body) or utc_iso(mtime),
            })
        return items, ("source = ?", (self.source,))

    def hashed_fields(self, item):
        return {"external_id": item["external_id"], "body": item["body"]}

    def chunk_texts(self, row):
        return chunk_text(row["body"], f"Note: {row['external_id']}")
