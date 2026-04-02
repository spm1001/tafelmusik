"""Anchored comments — self-anchoring messages over any text artifact.

A comment is a message with an optional content-addressed anchor (TextQuoteSelector)
into a target artifact. Comments thread via replies_to. The system stores and
retrieves; it does not interpret targets or render anchors.

No dependency on Yjs, pycrdt, or any CRDT. Pure Python + SQLite.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    created: float  # Unix timestamp
    target: str
    body: str
    quote: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    replies_to: str | None = None
    resolved: bool = False


@dataclass
class AnchorResult:
    """Where a comment's quote was found in the text."""

    comment_id: str
    start: int
    end: int
    confident: bool  # True = exact match, False = fuzzy


CONTEXT_CHARS = 30  # chars of prefix/suffix to capture


def capture_context(text: str, start: int, end: int) -> tuple[str, str]:
    """Extract prefix and suffix context around a range in text."""
    prefix = text[max(0, start - CONTEXT_CHARS) : start]
    suffix = text[end : end + CONTEXT_CHARS]
    return prefix, suffix


def anchor(
    text: str,
    quote: str,
    prefix: str | None = None,
    suffix: str | None = None,
) -> AnchorResult | None:
    """Find where a quote anchors in text. Returns None if not found.

    Strategy:
    1. Exact match. If unique, done.
    2. If multiple exact matches, disambiguate with prefix/suffix context.
    3. Fuzzy match (SequenceMatcher) if exact fails.
    4. Context recovery: quote gone, but prefix/suffix still in the doc — anchor between them.
    """
    if not quote:
        return None

    # Strategy 1: exact match
    matches = _find_all(text, quote)

    if len(matches) == 1:
        start = matches[0]
        return AnchorResult(
            comment_id="", start=start, end=start + len(quote), confident=True
        )

    if len(matches) > 1 and (prefix or suffix):
        # Strategy 2: disambiguate with context
        best = _disambiguate(text, quote, matches, prefix, suffix)
        if best is not None:
            return AnchorResult(
                comment_id="", start=best, end=best + len(quote), confident=True
            )

    if len(matches) > 1:
        # Multiple matches, no context to disambiguate — take first
        start = matches[0]
        return AnchorResult(
            comment_id="", start=start, end=start + len(quote), confident=False
        )

    # Strategy 3: fuzzy match on quote text
    fuzzy = _fuzzy_find(text, quote)
    if fuzzy is not None:
        start, end = fuzzy
        return AnchorResult(comment_id="", start=start, end=end, confident=False)

    # Strategy 4: quote is gone, but find the region via prefix/suffix context
    context_result = _find_by_context(text, prefix, suffix)
    if context_result is not None:
        return AnchorResult(
            comment_id="", start=context_result[0], end=context_result[1], confident=False
        )

    return None


def _find_by_context(
    text: str, prefix: str | None, suffix: str | None
) -> tuple[int, int] | None:
    """Find a region in text by its surrounding prefix and suffix.

    When the quoted text has been deleted or completely rewritten, the
    prefix and suffix may still be present. Find them and return the
    region between them.
    """
    if not prefix and not suffix:
        return None

    if prefix and suffix:
        # Find prefix, then look for suffix after it
        prefix_idx = text.find(prefix)
        if prefix_idx != -1:
            region_start = prefix_idx + len(prefix)
            suffix_idx = text.find(suffix, region_start)
            if suffix_idx != -1:
                return region_start, suffix_idx

    if prefix:
        # Just prefix — anchor at its end
        prefix_idx = text.find(prefix)
        if prefix_idx != -1:
            region_start = prefix_idx + len(prefix)
            # Take a reasonable chunk after the prefix
            region_end = min(region_start + 50, len(text))
            return region_start, region_end

    if suffix:
        # Just suffix — anchor at its start
        suffix_idx = text.find(suffix)
        if suffix_idx != -1:
            region_start = max(0, suffix_idx - 50)
            return region_start, suffix_idx

    return None


def _find_all(text: str, sub: str) -> list[int]:
    """Find all occurrences of sub in text."""
    positions = []
    start = 0
    while True:
        idx = text.find(sub, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _disambiguate(
    text: str,
    quote: str,
    positions: list[int],
    prefix: str | None,
    suffix: str | None,
) -> int | None:
    """Pick the match best surrounded by the expected prefix/suffix."""
    best_score = -1.0
    best_pos = None

    for pos in positions:
        score = 0.0
        if prefix:
            actual_prefix = text[max(0, pos - len(prefix)) : pos]
            score += SequenceMatcher(None, prefix, actual_prefix).ratio()
        if suffix:
            actual_suffix = text[pos + len(quote) : pos + len(quote) + len(suffix)]
            score += SequenceMatcher(None, suffix, actual_suffix).ratio()
        if score > best_score:
            best_score = score
            best_pos = pos

    # Require reasonable context match
    if best_score > 0.6:
        return best_pos
    return None


def _fuzzy_find(
    text: str, quote: str, threshold: float = 0.7
) -> tuple[int, int] | None:
    """Find the best fuzzy match for quote in text.

    Slides a window across text and scores similarity. Returns (start, end)
    of the best match, or None if below threshold.
    """
    quote_len = len(quote)
    if quote_len == 0 or len(text) == 0:
        return None

    best_ratio = 0.0
    best_start = 0

    # Slide windows of varying size around the quote length
    for window_size in range(
        max(1, quote_len - quote_len // 4), quote_len + quote_len // 4 + 1
    ):
        for start in range(0, len(text) - window_size + 1, max(1, window_size // 4)):
            candidate = text[start : start + window_size]
            ratio = SequenceMatcher(None, quote, candidate).quick_ratio()
            if ratio > best_ratio:
                # Confirm with full ratio
                ratio = SequenceMatcher(None, quote, candidate).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = start
                    best_end = start + window_size

    if best_ratio >= threshold:
        return best_start, best_end
    return None


# --- Storage ---

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    created REAL NOT NULL,
    target TEXT NOT NULL,
    body TEXT NOT NULL,
    quote TEXT,
    prefix TEXT,
    suffix TEXT,
    replies_to TEXT REFERENCES comments(id),
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments_target ON comments(target);
CREATE INDEX IF NOT EXISTS idx_comments_target_active
    ON comments(target) WHERE resolved = 0;
CREATE INDEX IF NOT EXISTS idx_comments_replies ON comments(replies_to);
"""


class CommentStore:
    """SQLite-backed comment storage."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def create(
        self,
        *,
        author: str,
        target: str,
        body: str,
        quote: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        replies_to: str | None = None,
    ) -> Comment:
        """Create a new comment."""
        comment_id = f"{author}-{uuid.uuid4().hex[:12]}"
        created = time.time()

        self.db.execute(
            "INSERT INTO comments (id, author, created, target, body, quote, prefix, suffix, replies_to, resolved)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (comment_id, author, created, target, body, quote, prefix, suffix, replies_to),
        )
        self.db.commit()

        return Comment(
            id=comment_id,
            author=author,
            created=created,
            target=target,
            body=body,
            quote=quote,
            prefix=prefix,
            suffix=suffix,
            replies_to=replies_to,
            resolved=False,
        )

    def get(self, comment_id: str) -> Comment | None:
        """Get a comment by ID."""
        row = self.db.execute(
            "SELECT * FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        return _row_to_comment(row) if row else None

    def resolve(self, comment_id: str) -> Comment | None:
        """Mark a comment as resolved."""
        self.db.execute(
            "UPDATE comments SET resolved = 1 WHERE id = ?", (comment_id,)
        )
        self.db.commit()
        return self.get(comment_id)

    def unresolve(self, comment_id: str) -> Comment | None:
        """Mark a comment as unresolved."""
        self.db.execute(
            "UPDATE comments SET resolved = 0 WHERE id = ?", (comment_id,)
        )
        self.db.commit()
        return self.get(comment_id)

    def list_for_target(self, target: str, *, include_resolved: bool = False) -> list[Comment]:
        """List comments for a target, ordered by creation time."""
        if include_resolved:
            rows = self.db.execute(
                "SELECT * FROM comments WHERE target = ? ORDER BY created",
                (target,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM comments WHERE target = ? AND resolved = 0 ORDER BY created",
                (target,),
            ).fetchall()
        return [_row_to_comment(r) for r in rows]

    def list_thread(self, root_id: str) -> list[Comment]:
        """List a comment and all its replies, depth-first."""
        root = self.get(root_id)
        if root is None:
            return []

        result = [root]
        self._collect_replies(root_id, result)
        return result

    def _collect_replies(self, parent_id: str, result: list[Comment]):
        rows = self.db.execute(
            "SELECT * FROM comments WHERE replies_to = ? ORDER BY created",
            (parent_id,),
        ).fetchall()
        for row in rows:
            comment = _row_to_comment(row)
            result.append(comment)
            self._collect_replies(comment.id, result)

    def update_anchor(
        self, comment_id: str, *, quote: str, prefix: str | None, suffix: str | None
    ):
        """Update the anchor fields after re-anchoring."""
        self.db.execute(
            "UPDATE comments SET quote = ?, prefix = ?, suffix = ? WHERE id = ?",
            (quote, prefix, suffix, comment_id),
        )
        self.db.commit()

    def reanchor_all(self, target: str, text: str) -> dict[str, str]:
        """Re-anchor all active comments for a target against current text.

        Returns dict mapping comment_id -> status:
        - "anchored": exact match found
        - "drifted": fuzzy match found, anchor updated
        - "orphaned": quote not found
        - "unanchored": comment has no quote (about the target, not a specific place)
        """
        comments = self.list_for_target(target)
        results: dict[str, str] = {}

        for comment in comments:
            if not comment.quote:
                results[comment.id] = "unanchored"
                continue

            result = anchor(text, comment.quote, comment.prefix, comment.suffix)

            if result is None:
                results[comment.id] = "orphaned"
                continue

            if result.confident:
                # Update context (text around it may have changed)
                prefix, suffix = capture_context(text, result.start, result.end)
                self.update_anchor(
                    comment.id, quote=comment.quote, prefix=prefix, suffix=suffix
                )
                results[comment.id] = "anchored"
            else:
                # Fuzzy match — update quote and context to current text
                new_quote = text[result.start : result.end]
                prefix, suffix = capture_context(text, result.start, result.end)
                self.update_anchor(
                    comment.id, quote=new_quote, prefix=prefix, suffix=suffix
                )
                results[comment.id] = "drifted"

        return results


def _row_to_comment(row: sqlite3.Row) -> Comment:
    return Comment(
        id=row["id"],
        author=row["author"],
        created=row["created"],
        target=row["target"],
        body=row["body"],
        quote=row["quote"],
        prefix=row["prefix"],
        suffix=row["suffix"],
        replies_to=row["replies_to"],
        resolved=bool(row["resolved"]),
    )
