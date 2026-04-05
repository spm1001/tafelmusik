"""Calute spike: hydrate → edit → flush → re-hydrate round-trip.

Validates the critical path for Phase 1 of calute (files on disk).
This is a proof-of-concept, not production code. It tests:

1. Hydrate: .md file → Y.Text (content populates correctly)
2. Edit: document.py operations work on hydrated content
3. Flush: Y.Text → .md file (content written correctly)
4. Re-hydrate: flushed .md → fresh Y.Text (round-trip preserves content)
5. CRDT log truncation: fresh Doc after flush has no history baggage
"""

from pathlib import Path

import pytest
from pycrdt import Doc, Text

from tafelmusik import authors, document


@pytest.fixture
def docs_dir(tmp_path):
    """Temporary docs directory."""
    d = tmp_path / "docs"
    d.mkdir()
    return d


def _new_doc() -> tuple[Doc, Text]:
    """Create a fresh Doc with content Text."""
    doc = Doc()
    text = doc.get("content", type=Text)
    return doc, text


def _hydrate_from_file(md_path: Path) -> tuple[Doc, Text]:
    """Hydrate a fresh Doc from a .md file. This is what the ASGI server will do."""
    doc, text = _new_doc()
    content = md_path.read_text()
    with doc.transaction():
        text += content
    return doc, text


def _flush_to_file(text: Text, md_path: Path) -> str:
    """Flush Y.Text to .md file. Returns content written."""
    content = str(text)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content)
    return content


class TestCaluteRoundTrip:
    """The critical path: file → CRDT → edit → flush → file → CRDT."""

    def test_hydrate_from_file(self, docs_dir):
        """A .md file hydrates into Y.Text with correct content."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\nThis is a test document.\n\n## Section A\n\nContent here.\n")

        doc, text = _hydrate_from_file(md)
        assert str(text) == md.read_text()

    def test_edit_hydrated_content(self, docs_dir):
        """document.py operations work on hydrated content."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\nIntro paragraph.\n\n## Section A\n\nOriginal content.\n")

        doc, text = _hydrate_from_file(md)

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

        doc, text = _hydrate_from_file(md)
        document.patch(text, "Original.", "Edited.", author=authors.CLAUDE)

        written = _flush_to_file(text, md)
        assert "Edited." in written
        assert md.read_text() == written

    def test_flush_creates_new_file(self, docs_dir):
        """Flush creates a new .md file (and parent dirs) for new docs."""
        doc, text = _new_doc()
        with doc.transaction(origin=authors.CLAUDE):
            text += "# New Document\n\nCreated in Tafelmusik.\n"

        md = docs_dir / "nested" / "new-doc.md"
        _flush_to_file(text, md)

        assert md.exists()
        assert "New Document" in md.read_text()

    def test_full_round_trip(self, docs_dir):
        """Hydrate → edit → flush → re-hydrate preserves content."""
        md = docs_dir / "report.md"
        original = "# Q4 Report\n\nRevenue grew 15%.\n\n## Results\n\nThe quick brown fox jumped.\n"
        md.write_text(original)

        # Phase 1: Hydrate
        doc1, text1 = _hydrate_from_file(md)
        assert str(text1) == original

        # Phase 2: Claude edits
        document.replace_section(
            text1,
            "## Results\n\nThe quick brown fox jumped over the lazy dog.\n",
            author=authors.CLAUDE,
        )

        # Phase 3: Claude responds to review with a patch
        document.patch(
            text1, "quick brown fox", "swift red fox", author=authors.CLAUDE
        )

        # Phase 4: Flush
        flushed = _flush_to_file(text1, md)
        assert "swift red fox" in flushed
        assert "jumped over the lazy dog" in flushed

        # Phase 5: Re-hydrate into fresh Doc
        doc2, text2 = _hydrate_from_file(md)
        assert str(text2) == flushed

        # The content survived the round-trip
        assert "swift red fox jumped over the lazy dog" in str(text2)

    def test_rehydrated_doc_is_editable(self, docs_dir):
        """A re-hydrated doc supports further editing (no CRDT baggage)."""
        md = docs_dir / "test.md"
        md.write_text("# Hello\n\n## Section A\n\nFirst draft.\n")

        # Round trip 1
        doc1, text1 = _hydrate_from_file(md)
        document.patch(text1, "First draft.", "Second draft.", author=authors.CLAUDE)
        _flush_to_file(text1, md)

        # Round trip 2
        doc2, text2 = _hydrate_from_file(md)
        assert "Second draft." in str(text2)

        # Can still edit
        document.patch(text2, "Second draft.", "Third draft.", author=authors.CLAUDE)
        assert "Third draft." in str(text2)

        # Can flush again
        _flush_to_file(text2, md)
        assert "Third draft." in md.read_text()

    def test_state_vector_drift_score(self):
        """State vector diff gives meaningful drift measurement."""
        doc, text = _new_doc()
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
