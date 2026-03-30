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


def replace_all(text: Text, content: str, *, author: str) -> None:
    """Replace all content, tagged with authorship.

    Wrapped in a single transaction with origin=author so the observer
    can distinguish Claude's edits from remote edits.
    """
    with text.doc.transaction(origin=author):
        del text[:]
        if content:
            text.insert(0, content, attrs={"author": author})


def replace_section(text: Text, new_content: str, *, author: str) -> bool:
    """Replace a markdown section identified by its heading, or append if not found.

    new_content must start with a markdown heading line (e.g. "## Design\\n...").
    The section extends from the heading to the next heading of equal or higher
    level (fewer #s), or to the end of the document.

    Raises ValueError on h1 headings — h1 sections span the entire document,
    so replace_section would silently destroy all content. Use replace_all instead.

    Returns True if an existing section was replaced, False if appended as new.
    """
    content = str(text)
    heading = new_content.split("\n", 1)[0].strip()
    level = heading_level(heading)
    if level == 1:
        raise ValueError(
            f"replace_section refuses h1 heading '{heading}' — it would replace "
            f"the entire document. Use replace_all instead."
        )
    bounds = find_section(content, heading)

    with text.doc.transaction(origin=author):
        if bounds is None:
            # Append with appropriate spacing
            if content and not content.endswith("\n\n"):
                separator = "\n\n" if not content.endswith("\n") else "\n"
            elif not content:
                separator = ""
            else:
                separator = ""
            insert_at = len(str(text))
            text.insert(insert_at, separator + new_content, attrs={"author": author})
            return False

        start, end = bounds
        del text[start:end]
        text.insert(start, new_content, attrs={"author": author})
        return True


def diff_sections(old: str, new: str) -> list[tuple[str, str]]:
    """Compare two markdown strings and return which sections changed.

    Returns a list of (heading, change_type) tuples where change_type is
    one of "added", "removed", or "modified".
    """
    old_sections = _extract_sections(old)
    new_sections = _extract_sections(new)
    old_headings = set(old_sections)
    new_headings = set(new_sections)

    changes = []
    for h in sorted(new_headings - old_headings):
        changes.append((h, "added"))
    for h in sorted(old_headings - new_headings):
        changes.append((h, "removed"))
    for h in sorted(old_headings & new_headings):
        if old_sections[h].rstrip() != new_sections[h].rstrip():
            changes.append((h, "modified"))

    # If no section-level changes but content differs, report as top-level edit
    if not changes and old != new:
        changes.append(("(document)", "modified"))

    return changes


def _extract_sections(content: str) -> dict[str, str]:
    """Extract a dict of heading → section content (including the heading line)."""
    fenced = _fenced_ranges(content)
    headings = []
    for m in _HEADING_RE.finditer(content):
        if not _in_fenced_block(m.start(), fenced):
            headings.append((m.start(), m.group().strip()))

    if not headings:
        return {}

    sections = {}
    for i, (start, heading) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(content)
        sections[heading] = content[start:end]
    return sections


def find_section(content: str, heading: str) -> tuple[int, int] | None:
    """Find section boundaries by heading text.

    Returns (start, end) character indices where start is the first character
    of the heading line and end is either the first character of the next heading
    at the same or higher level, or len(content).
    """
    heading = heading.strip()
    level = heading_level(heading)
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


def patch(text: Text, find: str, replace: str, *, author: str) -> None:
    """Content-addressed find-and-replace on a Y.Text.

    Matches `find` literally in the current text. Exactly one match is required —
    zero matches raises ValueError (text not found), two or more raises ValueError
    (ambiguous match). Only the matched range is deleted and replaced, so authorship
    attrs on surrounding text are preserved.
    """
    content = str(text)
    first = content.find(find)
    if first == -1:
        raise ValueError(f"patch: text not found: {find!r}")
    second = content.find(find, first + 1)
    if second != -1:
        raise ValueError(
            f"patch: ambiguous match — found {find!r} at positions {first} and {second}"
        )
    with text.doc.transaction(origin=author):
        del text[first : first + len(find)]
        if replace:
            text.insert(first, replace, attrs={"author": author})


def heading_level(line: str) -> int | None:
    """Return heading level (1-6) or None if not a markdown heading."""
    m = _HEADING_RE.match(line.strip())
    return len(m.group(1)) if m else None
