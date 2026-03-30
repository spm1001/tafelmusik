"""Calute spike: hydrate → edit → flush → re-hydrate round-trip.

Validates the critical path for Phase 1 of calute (files on disk).
This is a proof-of-concept, not production code. It tests:

1. Hydrate: .md file → Y.Text (content populates correctly)
2. Edit: document.py operations work on hydrated content
3. Comments: add comments, verify they anchor correctly
4. Flush: Y.Text → .md file (content written correctly)
5. Comment wipe: comments cleared on flush
6. Re-hydrate: flushed .md → fresh Y.Text (round-trip preserves content)
7. CRDT log truncation: fresh Doc after flush has no history baggage
"""

import json
from pathlib import Path

import pytest
from pycrdt import Assoc, Doc, Map, StickyIndex, Text

from tafelmusik import authors, comments, document


@pytest.fixture
def docs_dir(tmp_path):
    """Temporary docs directory."""
    d = tmp_path / "docs"
    d.mkdir()
    return d


def _new_doc() -> tuple[Doc, Text, Map]:
    """Create a fresh Doc with content Text and comments Map."""
    doc = Doc()
    text = doc.get("content", type=Text)
    comments_map = doc.get("comments", type=Map)
    return doc, text, comments_map


def _hydrate_from_file(md_path: Path) -> tuple[Doc, Text, Map]:
    """Hydrate a fresh Doc from a .md file. This is what the ASGI server will do."""
    doc, text, comments_map = _new_doc()
    content = md_path.read_text()
    with doc.transaction():
        text += content
    return doc, text, comments_map


def _flush_to_file(text: Text, comments_map: Map, md_path: Path) -> str:
    """Flush Y.Text to .md file and wipe comments. Returns content written."""
    content = str(text)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content)

    # Wipe comments
    with text.doc.transaction(origin=authors.CLAUDE):
        for key in list(comments_map):
            del comments_map[key]

    return content


def _add_comment(text: Text, comments_map: Map, quote: str, body: str) -> str:
    """Add a comment anchored to quote text. Returns comment ID."""
    content = str(text)
    idx = content.find(quote)
    assert idx != -1, f"Quote {quote!r} not found in document"

    comment_id = f"test-{len(comments_map)}"
    start_si = StickyIndex.new(text, idx, assoc=Assoc.AFTER)
    end_si = StickyIndex.new(text, idx + len(quote), assoc=Assoc.BEFORE)

    # Must integrate into Doc BEFORE setting properties (pycrdt requirement)
    inner = Map()
    comments_map[comment_id] = inner
    inner["anchorStart"] = json.dumps(start_si.to_json())
    inner["anchorEnd"] = json.dumps(end_si.to_json())
    inner["anchor"] = json.dumps(start_si.to_json())
    inner["quote"] = quote
    inner["body"] = body
    inner["author"] = "sameer"
    inner["resolved"] = False

    return comment_id


class TestCaluteRoundTrip:
    """The critical path: file → CRDT → edit → flush → file → CRDT."""

    def test_hydrate_from_file(self, docs_dir):
        """A .md file hydrates into Y.Text with correct content."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\nThis is a test document.\n\n## Section A\n\nContent here.\n")

        doc, text, _ = _hydrate_from_file(md)
        assert str(text) == md.read_text()

    def test_edit_hydrated_content(self, docs_dir):
        """document.py operations work on hydrated content."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\nIntro paragraph.\n\n## Section A\n\nOriginal content.\n")

        doc, text, _ = _hydrate_from_file(md)

        # replace_section
        document.replace_section(
            text,
            "## Section A\n\nUpdated content.\n",
            author=authors.CLAUDE,
        )
        assert "Updated content." in str(text)
        assert "Original content." not in str(text)

        # patch
        document.patch(text, "Intro paragraph.", "Revised intro.", author=authors.CLAUDE)
        assert "Revised intro." in str(text)

    def test_flush_writes_correct_content(self, docs_dir):
        """Flush writes current Y.Text to .md file."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\nOriginal.\n")

        doc, text, comments_map = _hydrate_from_file(md)
        document.patch(text, "Original.", "Edited.", author=authors.CLAUDE)

        written = _flush_to_file(text, comments_map, md)
        assert "Edited." in written
        assert md.read_text() == written

    def test_flush_creates_new_file(self, docs_dir):
        """Flush creates a new .md file (and parent dirs) for new docs."""
        doc, text, comments_map = _new_doc()
        with doc.transaction(origin=authors.CLAUDE):
            text += "# New Document\n\nCreated in Tafelmusik.\n"

        md = docs_dir / "nested" / "new-doc.md"
        _flush_to_file(text, comments_map, md)

        assert md.exists()
        assert "New Document" in md.read_text()

    def test_comments_wipe_on_flush(self, docs_dir):
        """Comments are cleared when flushing to file."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\nThe quick brown fox.\n")

        doc, text, comments_map = _hydrate_from_file(md)
        _add_comment(text, comments_map, "quick brown fox", "Change to fast")
        assert len(comments_map) == 1

        _flush_to_file(text, comments_map, md)
        assert len(comments_map) == 0

    def test_full_round_trip(self, docs_dir):
        """Hydrate → edit → comment → flush → re-hydrate preserves content."""
        md = docs_dir / "report.md"
        original = "# Q4 Report\n\nRevenue grew 15%.\n\n## Results\n\nThe quick brown fox jumped.\n"
        md.write_text(original)

        # Phase 1: Hydrate
        doc1, text1, comments1 = _hydrate_from_file(md)
        assert str(text1) == original

        # Phase 2: Claude edits
        document.replace_section(
            text1,
            "## Results\n\nThe quick brown fox jumped over the lazy dog.\n",
            author=authors.CLAUDE,
        )

        # Phase 3: Sameer comments
        _add_comment(text1, comments1, "quick brown fox", "Make this more specific")
        assert len(comments1) == 1

        # Phase 4: Claude responds to comment with a patch
        document.patch(
            text1, "quick brown fox", "swift red fox", author=authors.CLAUDE
        )

        # Phase 5: Flush
        flushed = _flush_to_file(text1, comments1, md)
        assert "swift red fox" in flushed
        assert "jumped over the lazy dog" in flushed
        assert len(comments1) == 0  # Comments wiped

        # Phase 6: Re-hydrate into fresh Doc
        doc2, text2, comments2 = _hydrate_from_file(md)
        assert str(text2) == flushed
        assert len(comments2) == 0  # No comments in fresh doc

        # The content survived the round-trip
        assert "swift red fox jumped over the lazy dog" in str(text2)

    def test_rehydrated_doc_is_editable(self, docs_dir):
        """A re-hydrated doc supports further editing (no CRDT baggage)."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\n## Section A\n\nFirst draft.\n")

        # Round trip 1
        doc1, text1, c1 = _hydrate_from_file(md)
        document.patch(text1, "First draft.", "Second draft.", author=authors.CLAUDE)
        _flush_to_file(text1, c1, md)

        # Round trip 2
        doc2, text2, c2 = _hydrate_from_file(md)
        assert "Second draft." in str(text2)

        # Can still edit
        document.patch(text2, "Second draft.", "Third draft.", author=authors.CLAUDE)
        assert "Third draft." in str(text2)

        # Can add comments to re-hydrated doc
        _add_comment(text2, c2, "Third draft", "Looks good")
        assert len(c2) == 1

        # Can flush again
        _flush_to_file(text2, c2, md)
        assert "Third draft." in md.read_text()

    def test_comment_reanchoring_survives_round_trip(self, docs_dir):
        """Comments re-anchor correctly during edits, then wipe on flush."""
        md = docs_dir / "test.md"
        md.write_text("# Doc\n\n## Section\n\nThe weather is nice today.\n")

        doc, text, comments_map = _hydrate_from_file(md)
        _add_comment(text, comments_map, "weather is nice", "Be more specific")

        # Collect affected before edit
        bounds = document.find_section(str(text), "## Section")
        assert bounds is not None
        affected = comments.collect_affected(text, comments_map, bounds[0], bounds[1])
        assert len(affected) == 1

        # Edit the section
        document.replace_section(
            text,
            "## Section\n\nThe weather is nice today. Very sunny.\n",
            author=authors.CLAUDE,
        )

        # Re-anchor within new section bounds
        new_bounds = document.find_section(str(text), "## Section")
        result = comments.reanchor(
            text, comments_map, affected, new_bounds[0], new_bounds[1], author=authors.CLAUDE
        )
        assert result["reanchored"] == [affected[0]["id"]]
        assert result["orphaned"] == []

        # Flush wipes everything
        _flush_to_file(text, comments_map, md)
        assert len(comments_map) == 0
        assert "Very sunny." in md.read_text()

    def test_state_vector_drift_score(self):
        """State vector diff gives meaningful drift measurement."""
        doc, text, _ = _new_doc()
        with doc.transaction():
            text += "Initial content.\n"

        # Snapshot state vector
        snapshot = doc.get_state()

        # Make some edits
        document.patch(text, "Initial content.", "Edited content.", author=authors.CLAUDE)

        # Drift = byte size of update since snapshot
        drift_update = doc.get_update(snapshot)
        assert len(drift_update) > 0, "Should have non-zero drift after edit"

        # Snapshot again
        snapshot2 = doc.get_state()

        # No further edits = zero drift
        no_drift = doc.get_update(snapshot2)
        # Yjs always returns a minimal update even with no changes,
        # but it should be very small
        assert len(no_drift) < len(drift_update), "No-change drift should be smaller"
