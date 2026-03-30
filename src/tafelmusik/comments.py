"""Y.Map comment operations — point+quote StickyIndex anchoring."""

from __future__ import annotations

import json

from pycrdt import Assoc, Map, StickyIndex, Text


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
