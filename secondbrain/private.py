"""Strip `# private` sections from Markdown before anything else sees the text.

A section starts at an H1 line reading exactly `# private` (any case, surrounding
whitespace allowed) and runs until the next H1. Everything in between, including
`##`-and-deeper subheaders, is dropped along with the `# private` line itself.

Lines inside fenced code blocks are never headers, so a `# comment` in a code block
neither starts nor ends a private section. When unsure, this strips more, not less:
an unclosed fence inside a private section keeps the rest of the note private.
"""
import re

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_H1 = re.compile(r"^ {0,3}#(?:[ \t]|$)")
_PRIVATE = re.compile(r"^ {0,3}#[ \t]+private[ \t]*#*[ \t]*$", re.IGNORECASE)


def strip_private(text: str) -> str:
    out, private, fence = [], False, None
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        m = _FENCE.match(bare)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not bare.strip().lstrip(fence[0]):
                fence = None
        elif m:
            fence = m.group(1)
        elif _PRIVATE.match(bare):
            private = True
            continue
        elif _H1.match(bare):
            private = False
        if not private:
            out.append(line)
    return "".join(out)
