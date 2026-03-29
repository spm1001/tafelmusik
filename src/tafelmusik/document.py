"""Y.Text operations — read, edit, replace_section. Shared by MCP and channel servers."""

from __future__ import annotations

import re

from pycrdt import Text

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)


def _fenced_ranges(content: str) -> list[tuple[int, int]]:
    """Character ranges inside fenced code blocks (``` or ~~~).

    Pairs opening and closing fences by matching character and minimum
    length per CommonMark rules. Unclosed fences extend to end of document.
    """
    ranges = []
    fence_open = None
    fence_char = None
    fence_len = 0
    for m in _FENCE_RE.finditer(content):
        char = m.group(1)[0]
        length = len(m.group(1))
        if fence_open is None:
            fence_open = m.start()
            fence_char = char
            fence_len = length
        elif char == fence_char and length >= fence_len:
            line_end = content.find("\n", m.end())
            ranges.append((fence_open, line_end if line_end != -1 else len(content)))
            fence_open = None
    if fence_open is not None:
        ranges.append((fence_open, len(content)))
    return ranges


def _in_fenced_block(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """Check if a character position falls inside a fenced code block."""
    return any(start <= pos < end for start, end in ranges)


def read(text: Text) -> str:
    """Read the full content of a Y.Text as a plain string."""
    return str(text)


def replace_all(text: Text, content: str) -> None:
    """Replace all content. Uses slice assignment for a single CRDT transaction."""
    text[:] = content


def replace_section(text: Text, new_content: str) -> bool:
    """Replace a markdown section identified by its heading, or append if not found.

    new_content must start with a markdown heading line (e.g. "## Design\\n...").
    The section extends from the heading to the next heading of equal or higher
    level (fewer #s), or to the end of the document.

    Returns True if an existing section was replaced, False if appended as new.
    """
    content = str(text)
    heading = new_content.split("\n", 1)[0].strip()
    bounds = find_section(content, heading)

    if bounds is None:
        # Append with appropriate spacing
        if content and not content.endswith("\n\n"):
            separator = "\n\n" if not content.endswith("\n") else "\n"
        elif not content:
            separator = ""
        else:
            separator = ""
        text += separator + new_content
        return False

    start, end = bounds
    text[start:end] = new_content
    return True


def find_section(content: str, heading: str) -> tuple[int, int] | None:
    """Find section boundaries by heading text.

    Returns (start, end) character indices where start is the first character
    of the heading line and end is either the first character of the next heading
    at the same or higher level, or len(content).
    """
    heading = heading.strip()
    level = _heading_level(heading)
    if level is None:
        return None

    fenced = _fenced_ranges(content)

    # Find the heading in the document (skip fenced code blocks)
    start = None
    for m in _HEADING_RE.finditer(content):
        if m.group().strip() == heading and not _in_fenced_block(m.start(), fenced):
            start = m.start()
            break

    if start is None:
        return None

    # Find the end: next heading of same or higher level (skip fenced)
    heading_end = content.find("\n", start)
    if heading_end == -1:
        # Heading is the last line with no trailing newline
        return (start, len(content))
    search_from = heading_end + 1

    for m in _HEADING_RE.finditer(content, search_from):
        if len(m.group(1)) <= level and not _in_fenced_block(m.start(), fenced):
            return (start, m.start())

    return (start, len(content))


def _heading_level(line: str) -> int | None:
    """Return heading level (1-6) or None if not a markdown heading."""
    m = _HEADING_RE.match(line.strip())
    return len(m.group(1)) if m else None
