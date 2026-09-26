"""Deterministic chunking (DESIGN.md §5). No overlap, no LLM."""
import re

MAX_CHARS = 1500
_HEADING = re.compile(r"^#{1,6} ", re.M)


def _sections(text):
    starts = [m.start() for m in _HEADING.finditer(text)]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


def _pieces(text, limit):
    for section in _sections(text):
        for para in re.split(r"\n\s*\n", section):
            para = para.strip()
            while len(para) > limit:  # hard-split oversized paragraphs
                yield para[:limit]
                para = para[limit:]
            if para:
                yield para


def chunk_text(text: str, header: str, limit: int = MAX_CHARS) -> list[str]:
    """Pack paragraphs into chunks of <= limit chars, each prefixed with `header`."""
    budget = max(200, limit - len(header) - 1)
    chunks, cur = [], ""
    for piece in _pieces(text, budget):
        if cur and len(cur) + 2 + len(piece) > budget:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return [f"{header}\n{c}" for c in chunks] or [header]
