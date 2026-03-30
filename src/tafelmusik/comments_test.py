"""Tests for comment operations and StickyIndex anchoring."""

import json

from pycrdt import Doc, Map, StickyIndex, Text

from tafelmusik import authors, comments, document


def _make_doc(content: str = "") -> tuple[Text, Map]:
    """Create a Doc with Y.Text and Y.Map for comments."""
    doc = Doc()
    doc["content"] = text = Text()
    doc["comments"] = comments_map = Map()
    if content:
        text += content
    return text, comments_map


def _add_comment(
    text: Text,
    comments_map: Map,
    quote: str,
    body: str,
    *,
    author: str = "sameer",
    comment_id: str | None = None,
) -> str:
    """Add a comment anchored to quote text. Returns comment ID."""
    cid = comment_id or f"test-{id(body)}"
    return comments.add_comment(
        text.doc, text, comments_map, quote, body, author=author, comment_id=cid
    )


# --- collect_affected ---


def test_collect_skips_resolved():
    text, cmap = _make_doc("Hello world")
    cid = _add_comment(text, cmap, "Hello", "hi")
    with text.doc.transaction():
        cmap[cid]["resolved"] = True
    result = comments.collect_affected(text, cmap, 0, len(str(text)))
    assert len(result) == 0


def test_collect_returns_active_in_range():
    text, cmap = _make_doc("Hello world")
    cid = _add_comment(text, cmap, "Hello", "hi")
    result = comments.collect_affected(text, cmap, 0, len(str(text)))
    assert len(result) == 1
    assert result[0]["id"] == cid
    assert result[0]["quote"] == "Hello"


def test_collect_excludes_outside_range():
    """Comments outside the section range are not collected."""
    text, cmap = _make_doc("## A\n\nFirst.\n\n## B\n\nSecond.\n")
    _add_comment(text, cmap, "Second", "note", comment_id="c1")
    # Only collect from section A (chars 0-14ish)
    bounds = document.find_section(str(text), "## A")
    result = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    assert len(result) == 0  # c1 is in section B, not A


def test_collect_includes_inside_range():
    """Comments inside the section range are collected."""
    text, cmap = _make_doc("## A\n\nFirst.\n\n## B\n\nSecond.\n")
    _add_comment(text, cmap, "First", "note", comment_id="c1")
    bounds = document.find_section(str(text), "## A")
    result = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    assert len(result) == 1
    assert result[0]["id"] == "c1"


def test_collect_skips_corrupted_anchor():
    """Comments with invalid anchor JSON are skipped with a warning, not crash."""
    text, cmap = _make_doc("Hello world")
    # Manually create a comment with garbage anchor
    with text.doc.transaction():
        comment = Map()
        cmap["bad"] = comment
        comment["anchor"] = "not valid json {"
        comment["quote"] = "Hello"
        comment["author"] = "sameer"
        comment["body"] = "test"
        comment["resolved"] = False
    result = comments.collect_affected(text, cmap, 0, len(str(text)))
    assert len(result) == 0  # skipped, not crashed


# --- reanchor: quote survives ---


def test_reanchor_quote_survives_replace_section():
    """Comment is re-anchored when its quote text survives a section rewrite."""
    text, cmap = _make_doc("## Design\n\nThe API uses REST.\n\n## Usage\n\nUse it.\n")
    _add_comment(text, cmap, "The API uses REST", "Consider GraphQL", comment_id="c1")

    bounds = document.find_section(str(text), "## Design")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    document.replace_section(
        text, "## Design\n\nThe API uses REST and GraphQL.\n", author=authors.TEST
    )
    new_bounds = document.find_section(str(text), "## Design")
    result = comments.reanchor(
        text,
        cmap,
        affected,
        search_start=new_bounds[0],
        search_end=new_bounds[1],
        author=authors.TEST,
    )

    assert "c1" in result["reanchored"]
    assert "c1" not in result["orphaned"]
    assert cmap["c1"].get("orphaned") is None or cmap["c1"].get("orphaned") is False


def test_reanchor_rebuilds_valid_anchors():
    """After re-anchoring, the anchorStart/anchorEnd resolve to the quote's new position."""
    text, cmap = _make_doc("## Notes\n\nKeep this line.\n\n## End\n")
    _add_comment(text, cmap, "Keep this line", "important", comment_id="c1")

    bounds = document.find_section(str(text), "## Notes")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    document.replace_section(
        text, "## Notes\n\nNew intro.\n\nKeep this line.\n", author=authors.TEST
    )
    new_bounds = document.find_section(str(text), "## Notes")
    result = comments.reanchor(
        text,
        cmap,
        affected,
        search_start=new_bounds[0],
        search_end=new_bounds[1],
        author=authors.TEST,
    )
    assert "c1" in result["reanchored"]

    # Verify the new anchor resolves to the right position
    new_content = str(text)
    expected_idx = new_content.find("Keep this line")
    assert expected_idx != -1

    anchor_json = json.loads(cmap["c1"]["anchorStart"])
    si = StickyIndex.from_json(anchor_json, text)
    assert si.get_index() == expected_idx


# --- reanchor: quote deleted → orphaned ---


def test_reanchor_quote_deleted_orphans():
    """Comment is orphaned when its quote text no longer exists."""
    text, cmap = _make_doc("## Design\n\nUse pagination.\n\n## Usage\n\nUse it.\n")
    _add_comment(text, cmap, "Use pagination", "Add offset/limit", comment_id="c1")

    bounds = document.find_section(str(text), "## Design")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    document.replace_section(text, "## Design\n\nResults are streamed.\n", author=authors.TEST)
    new_bounds = document.find_section(str(text), "## Design")
    result = comments.reanchor(
        text,
        cmap,
        affected,
        search_start=new_bounds[0],
        search_end=new_bounds[1],
        author=authors.TEST,
    )

    assert "c1" in result["orphaned"]
    assert "c1" not in result["reanchored"]
    assert cmap["c1"]["orphaned"] is True


# --- reanchor: mixed survival ---


def test_reanchor_mixed_survival():
    """One comment survives, another is orphaned in the same replace."""
    text, cmap = _make_doc("## API\n\nThe API uses REST.\n\nIt supports pagination.\n\n## End\n")
    _add_comment(text, cmap, "The API uses REST", "GraphQL?", comment_id="c1")
    _add_comment(text, cmap, "supports pagination", "offset/limit?", comment_id="c2")

    bounds = document.find_section(str(text), "## API")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    document.replace_section(
        text,
        "## API\n\nThe API uses REST and GraphQL.\n\nResults are streamed.\n",
        author=authors.TEST,
    )
    new_bounds = document.find_section(str(text), "## API")
    result = comments.reanchor(
        text,
        cmap,
        affected,
        search_start=new_bounds[0],
        search_end=new_bounds[1],
        author=authors.TEST,
    )

    assert "c1" in result["reanchored"]  # "The API uses REST" survived
    assert "c2" in result["orphaned"]  # "supports pagination" gone


# --- comment outside blast radius is untouched ---


def test_comment_outside_section_not_collected():
    """Comment in a different section is not even collected — CRDT handles it."""
    text, cmap = _make_doc("## API\n\nOld API text.\n\n## Usage\n\nUse it carefully.\n")
    _add_comment(text, cmap, "Use it carefully", "More detail needed", comment_id="c1")

    # Collect only from ## API
    bounds = document.find_section(str(text), "## API")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])

    # c1 is in ## Usage — not affected
    assert len(affected) == 0


# --- reanchor: replace_all ---


def test_reanchor_replace_all_preserves_surviving():
    text, cmap = _make_doc("Hello world, goodbye world")
    _add_comment(text, cmap, "Hello world", "greeting", comment_id="c1")
    _add_comment(text, cmap, "goodbye world", "farewell", comment_id="c2")

    affected = comments.collect_affected(text, cmap, 0, len(str(text)))
    document.replace_all(text, "Hello world, new content", author=authors.TEST)
    result = comments.reanchor(text, cmap, affected, author=authors.TEST)

    assert "c1" in result["reanchored"]
    assert "c2" in result["orphaned"]


# --- reanchor: resolved comment ignored ---


def test_reanchor_ignores_resolved():
    """Resolved comments are not collected."""
    text, cmap = _make_doc("## Notes\n\nSome text.\n")
    cid = _add_comment(text, cmap, "Some text", "resolved comment", comment_id="c1")
    with text.doc.transaction():
        cmap[cid]["resolved"] = True

    affected = comments.collect_affected(text, cmap, 0, len(str(text)))
    assert len(affected) == 0


# --- reanchor: clears orphaned flag on re-anchor ---


def test_reanchor_clears_orphaned_flag():
    """If a previously orphaned comment's quote reappears, orphaned flag is cleared."""
    text, cmap = _make_doc("## Notes\n\nKeep this.\n")
    _add_comment(text, cmap, "Keep this", "important", comment_id="c1")

    # First edit: orphan it
    bounds = document.find_section(str(text), "## Notes")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    document.replace_section(text, "## Notes\n\nNew text.\n", author=authors.TEST)
    new_bounds = document.find_section(str(text), "## Notes")
    comments.reanchor(
        text,
        cmap,
        affected,
        search_start=new_bounds[0],
        search_end=new_bounds[1],
        author=authors.TEST,
    )
    assert cmap["c1"]["orphaned"] is True

    # Second edit: bring back the quote — orphaned comment is still collected
    # because orphaned != resolved
    bounds2 = document.find_section(str(text), "## Notes")
    affected2 = comments.collect_affected(text, cmap, bounds2[0], bounds2[1])
    document.replace_section(text, "## Notes\n\nKeep this.\n", author=authors.TEST)
    new_bounds2 = document.find_section(str(text), "## Notes")
    result = comments.reanchor(
        text,
        cmap,
        affected2,
        search_start=new_bounds2[0],
        search_end=new_bounds2[1],
        author=authors.TEST,
    )
    assert "c1" in result["reanchored"]
    assert cmap["c1"].get("orphaned") is False


# --- false match prevention ---


def test_no_false_match_outside_section():
    """Quote text appearing in another section does NOT cause a false re-anchor."""
    text, cmap = _make_doc(
        "## Step 1\n\nThe weather is nice.\n\n## Step 2\n\nThe weather comment should orphan.\n"
    )
    _add_comment(text, cmap, "The weather", "note about weather", comment_id="c1")

    # Replace Step 1 — removes "The weather is nice" but "The weather" exists in Step 2
    bounds = document.find_section(str(text), "## Step 1")
    affected = comments.collect_affected(text, cmap, bounds[0], bounds[1])
    document.replace_section(
        text, "## Step 1\n\nSomething completely different.\n", author=authors.TEST
    )
    new_bounds = document.find_section(str(text), "## Step 1")
    result = comments.reanchor(
        text,
        cmap,
        affected,
        search_start=new_bounds[0],
        search_end=new_bounds[1],
        author=authors.TEST,
    )

    # Should orphan — not false-match against Step 2
    assert "c1" in result["orphaned"]
    assert "c1" not in result["reanchored"]


# --- patch mode re-anchoring ---


def test_reanchor_patch_orphans_deleted_quote():
    """Patch that deletes commented text orphans the comment."""
    text, cmap = _make_doc("The quick brown fox jumps over the lazy dog.")
    _add_comment(text, cmap, "brown fox", "change animal", comment_id="c1")

    content = str(text)
    find = "brown fox"
    patch_start = content.find(find)
    affected = comments.collect_affected(text, cmap, patch_start, patch_start + len(find))
    document.patch(text, find, "red cat", author=authors.TEST)
    result = comments.reanchor(
        text,
        cmap,
        affected,
        search_start=patch_start,
        search_end=patch_start + len("red cat"),
        author=authors.TEST,
    )

    # "brown fox" is gone — comment orphaned
    assert "c1" in result["orphaned"]


def test_reanchor_patch_preserves_surviving_quote():
    """Patch near a comment that doesn't touch the quoted text — comment survives via CRDT."""
    text, cmap = _make_doc("The quick brown fox jumps over the lazy dog.")
    _add_comment(text, cmap, "brown fox", "nice animal", comment_id="c1")

    # Patch "lazy" → "energetic" — doesn't touch "brown fox"
    content = str(text)
    find = "lazy"
    patch_start = content.find(find)
    affected = comments.collect_affected(text, cmap, patch_start, patch_start + len(find))

    # "brown fox" is outside the patch range — not collected
    assert len(affected) == 0


# --- add_comment ---


def test_add_comment_anchors_and_stores():
    """add_comment creates StickyIndex anchors and populates all Y.Map fields."""
    text, cmap = _make_doc("Hello world")
    cid = comments.add_comment(
        text.doc, text, cmap, "Hello", "greeting", author="sameer", comment_id="c1"
    )
    assert cid == "c1"
    c = cmap["c1"]
    assert c["quote"] == "Hello"
    assert c["body"] == "greeting"
    assert c["author"] == "sameer"
    assert c["resolved"] is False
    assert c["anchorStart"]  # non-empty JSON string
    assert c["anchorEnd"]
    assert c["anchor"]


def test_add_comment_generates_id():
    """Without explicit comment_id, generates author-prefixed ID."""
    text, cmap = _make_doc("Hello world")
    cid = comments.add_comment(
        text.doc, text, cmap, "Hello", "greeting", author="sameer"
    )
    assert cid.startswith("sameer-")


def test_add_comment_raises_on_missing_quote():
    """add_comment raises ValueError when quote text is not in the document."""
    text, cmap = _make_doc("Hello world")
    try:
        comments.add_comment(
            text.doc, text, cmap, "nonexistent", "body", author="sameer"
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "nonexistent" in str(e)


# --- list_comments ---


def test_list_comments_empty():
    """Empty comments map returns empty list."""
    _, cmap = _make_doc("Hello")
    assert comments.list_comments(cmap) == []


def test_list_comments_returns_all_fields():
    """list_comments returns all expected fields sorted by creation time."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "first", comment_id="c1")
    _add_comment(text, cmap, "world", "second", comment_id="c2")
    entries = comments.list_comments(cmap)
    assert len(entries) == 2
    for e in entries:
        assert set(e.keys()) == {"id", "author", "quote", "body", "resolved", "orphaned", "created"}


def test_list_comments_includes_resolved():
    """Resolved comments appear in the list (with resolved=True)."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "greeting", comment_id="c1")
    with text.doc.transaction():
        cmap["c1"]["resolved"] = True
    entries = comments.list_comments(cmap)
    assert len(entries) == 1
    assert entries[0]["resolved"] is True


# --- resolve_comment ---


def test_resolve_comment_by_quote():
    """resolve_comment marks matching unresolved comment as resolved."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "greeting", comment_id="c1")
    count = comments.resolve_comment(text.doc, cmap, "Hello", author=authors.CLAUDE)
    assert count == 1
    assert cmap["c1"]["resolved"] is True


def test_resolve_comment_no_match():
    """resolve_comment returns 0 when no comment matches the quote."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "greeting", comment_id="c1")
    count = comments.resolve_comment(text.doc, cmap, "nonexistent", author=authors.CLAUDE)
    assert count == 0
    assert cmap["c1"]["resolved"] is False


def test_resolve_comment_skips_already_resolved():
    """Already-resolved comments are not counted again."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "greeting", comment_id="c1")
    with text.doc.transaction():
        cmap["c1"]["resolved"] = True
    count = comments.resolve_comment(text.doc, cmap, "Hello", author=authors.CLAUDE)
    assert count == 0


def test_resolve_comment_strips_whitespace():
    """Quote matching strips leading/trailing whitespace."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "greeting", comment_id="c1")
    count = comments.resolve_comment(text.doc, cmap, "  Hello  ", author=authors.CLAUDE)
    assert count == 1


# --- clear_all ---


def test_clear_all_removes_comments():
    """clear_all deletes all comments from the Y.Map."""
    text, cmap = _make_doc("Hello world")
    _add_comment(text, cmap, "Hello", "first", comment_id="c1")
    _add_comment(text, cmap, "world", "second", comment_id="c2")
    count = comments.clear_all(text.doc, cmap, author=authors.CLAUDE)
    assert count == 2
    assert len(list(cmap)) == 0


def test_clear_all_empty_map():
    """clear_all on empty map returns 0."""
    text, cmap = _make_doc("Hello")
    count = comments.clear_all(text.doc, cmap, author=authors.CLAUDE)
    assert count == 0
