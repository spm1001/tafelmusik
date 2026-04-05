"""Y.Map comment operations — point+quote StickyIndex anchoring.

Dead code: both MCP tools and the browser now use HTTP/SQLite comments.
Only clear_all is still called (by flush_doc), but it clears an empty
Y.Map — no surface writes Y.Map comments anymore. Entire module is
deletable once tfm-kokudo lands.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from pycrdt import Assoc, Doc, Map, StickyIndex, Text

log = logging.getLogger(__name__)


def collect_affected(
    text: Text,
    comments_map: Map,
    section_start: int,
    section_end: int,
) -> list[dict]:
    """Collect comments whose anchors fall within a character range.

    Uses StickyIndex resolution to determine each comment's position in the
    document. Only comments anchored inside [section_start, section_end) are
    returned — the CRDT already tracks everything outside the blast radius.

    Call this BEFORE a destructive edit.
    """
    entries = []
    for comment_id in comments_map:
        comment = comments_map[comment_id]
        if not isinstance(comment, Map):
            continue
        if comment.get("resolved"):
            continue
        quote = comment.get("quote", "")
        if not quote:
            continue

        # Resolve the anchor position via StickyIndex
        anchor_json_str = comment.get("anchor") or comment.get("anchorStart")
        if not anchor_json_str:
            continue
        try:
            si = StickyIndex.from_json(json.loads(anchor_json_str), text)
            pos = si.get_index()
        except Exception:
            log.warning("comment %s: failed to resolve anchor, skipping", comment_id)
            continue

        if section_start <= pos < section_end:
            entries.append(
                {
                    "id": comment_id,
                    "quote": quote,
                }
            )
    return entries


def reanchor(
    text: Text,
    comments_map: Map,
    affected: list[dict],
    search_start: int = 0,
    search_end: int | None = None,
    *,
    author: str,
) -> dict:
    """Re-anchor comments whose quote text can be found in the search region.

    Searches only within [search_start, search_end) of the current document.
    This prevents false matches in unrelated sections.

    For each comment in `affected`:
    - If `quote` is found in the search region, rebuild StickyIndex anchors.
    - If not found, mark the comment as orphaned.

    Returns {"reanchored": [...ids], "orphaned": [...ids]}.
    """
    content = str(text)
    if search_end is None:
        search_end = len(content)
    region = content[search_start:search_end]

    reanchored = []
    orphaned = []

    with text.doc.transaction(origin=author):
        for entry in affected:
            comment_id = entry["id"]
            quote = entry["quote"]

            # Check the comment still exists and is unresolved
            comment = comments_map.get(comment_id)
            if not isinstance(comment, Map):
                continue
            if comment.get("resolved"):
                continue

            idx = region.find(quote)
            if idx == -1:
                # Quote text gone from this region — orphan the comment
                comment["orphaned"] = True
                orphaned.append(comment_id)
            else:
                # Rebuild anchors at the absolute position
                abs_idx = search_start + idx
                start_si = StickyIndex.new(text, abs_idx, assoc=Assoc.AFTER)
                end_si = StickyIndex.new(text, abs_idx + len(quote), assoc=Assoc.BEFORE)
                comment["anchorStart"] = json.dumps(start_si.to_json())
                comment["anchorEnd"] = json.dumps(end_si.to_json())
                comment["anchor"] = json.dumps(start_si.to_json())
                # Clear orphaned flag if previously set
                if comment.get("orphaned"):
                    comment["orphaned"] = False
                reanchored.append(comment_id)

    return {"reanchored": reanchored, "orphaned": orphaned}


def add_comment(
    doc: Doc,
    text: Text,
    comments_map: Map,
    quote: str,
    body: str,
    *,
    author: str,
    comment_id: str | None = None,
) -> str:
    """Add a comment anchored to quote text in the document.

    Creates StickyIndex anchors at the quote's position and stores the
    comment in the Y.Map. Returns the comment ID.

    Raises ValueError if quote is not found in the document.
    """
    doc_text = str(text)
    idx = doc_text.find(quote)
    if idx == -1:
        raise ValueError(f"Quote not found in document: {quote!r}")

    start_si = StickyIndex.new(text, idx, assoc=Assoc.AFTER)
    end_si = StickyIndex.new(text, idx + len(quote), assoc=Assoc.BEFORE)

    cid = comment_id or f"{author}-{int(time.time() * 1000)}"

    with doc.transaction(origin=author):
        comment = Map()
        comments_map[cid] = comment
        comment["anchorStart"] = json.dumps(start_si.to_json())
        comment["anchorEnd"] = json.dumps(end_si.to_json())
        comment["anchor"] = json.dumps(start_si.to_json())
        comment["quote"] = quote
        comment["author"] = author
        comment["body"] = body
        comment["resolved"] = False
        comment["created"] = datetime.now(timezone.utc).isoformat()

    return cid


def list_comments(comments_map: Map) -> list[dict]:
    """List all comments from the Y.Map, sorted by creation time.

    Returns list of dicts with: id, author, quote, body, resolved, orphaned, created.
    """
    entries = []
    for comment_id in comments_map:
        comment = comments_map[comment_id]
        if not isinstance(comment, Map):
            continue
        entries.append(
            {
                "id": comment_id,
                "author": comment.get("author", "unknown"),
                "quote": comment.get("quote", ""),
                "body": comment.get("body", ""),
                "resolved": comment.get("resolved", False),
                "orphaned": comment.get("orphaned", False),
                "created": comment.get("created", ""),
            }
        )
    entries.sort(key=lambda e: e["created"])
    return entries


def clear_all(doc: Doc, comments_map: Map, *, author: str) -> int:
    """Clear all comments from the Y.Map. Returns count cleared."""
    keys = list(comments_map)
    if keys:
        with doc.transaction(origin=author):
            for key in keys:
                del comments_map[key]
    return len(keys)


def resolve_comment(
    doc: Doc,
    comments_map: Map,
    quote: str,
    *,
    author: str,
) -> int:
    """Resolve comments matching quote text. Returns count resolved."""
    resolved_count = 0
    for comment_id in comments_map:
        comment = comments_map[comment_id]
        if not isinstance(comment, Map):
            continue
        if comment.get("resolved"):
            continue
        if comment.get("quote", "").strip() == quote.strip():
            with doc.transaction(origin=author):
                comment["resolved"] = True
            resolved_count += 1
    return resolved_count
