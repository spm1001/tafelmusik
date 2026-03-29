"""Tests for Y.Text document operations."""

from pycrdt import Doc, Text

from tafelmusik import document


def _make_text(content: str = "") -> Text:
    """Create a Y.Text integrated into a Doc, optionally pre-filled."""
    doc = Doc()
    doc["content"] = text = Text()
    if content:
        text += content
    return text


# --- read ---


def test_read_empty():
    text = _make_text()
    assert document.read(text) == ""


def test_read_content():
    text = _make_text("Hello, world!")
    assert document.read(text) == "Hello, world!"


# --- replace_all ---


def test_replace_all_empty_to_content():
    text = _make_text()
    document.replace_all(text, "New content")
    assert str(text) == "New content"


def test_replace_all_content_to_content():
    text = _make_text("Old stuff")
    document.replace_all(text, "New stuff")
    assert str(text) == "New stuff"


def test_replace_all_content_to_empty():
    text = _make_text("Something here")
    document.replace_all(text, "")
    assert str(text) == ""


# --- find_section ---


def test_find_section_simple():
    content = "# Title\n\nIntro\n\n## Section A\n\nBody A\n\n## Section B\n\nBody B\n"
    start, end = document.find_section(content, "## Section A")
    assert content[start:end] == "## Section A\n\nBody A\n\n"


def test_find_section_last():
    content = "# Title\n\nIntro\n\n## Section A\n\nBody A\n\n## Section B\n\nBody B\n"
    start, end = document.find_section(content, "## Section B")
    assert content[start:end] == "## Section B\n\nBody B\n"
    assert end == len(content)


def test_find_section_not_found():
    content = "# Title\n\nIntro\n"
    assert document.find_section(content, "## Missing") is None


def test_find_section_h1_contains_h2():
    content = "# Top\n\nIntro\n\n## Sub\n\nSub body\n"
    start, end = document.find_section(content, "# Top")
    # h1 section spans the entire document because h2 is a lower level
    assert content[start:end] == content


def test_find_section_respects_level():
    content = "## A\n\nBody A\n\n### A.1\n\nNested\n\n## B\n\nBody B\n"
    start, end = document.find_section(content, "## A")
    # ## A includes ### A.1 (lower level) but stops at ## B (same level)
    assert content[start:end] == "## A\n\nBody A\n\n### A.1\n\nNested\n\n"


def test_find_section_heading_only_no_newline():
    content = "## Heading"
    start, end = document.find_section(content, "## Heading")
    assert (start, end) == (0, len(content))


def test_find_section_not_a_heading():
    content = "# Title\n\nSome text\n"
    assert document.find_section(content, "Some text") is None


# --- replace_section ---


def test_replace_section_existing():
    text = _make_text("# Title\n\nIntro\n\n## API\n\nOld API docs\n\n## Usage\n\nUsage text\n")
    replaced = document.replace_section(text, "## API\n\nNew API docs\n")
    assert replaced is True
    result = str(text)
    assert "New API docs" in result
    assert "Old API docs" not in result
    assert "## Usage\n\nUsage text\n" in result


def test_replace_section_append_when_missing():
    text = _make_text("# Title\n\nIntro\n")
    replaced = document.replace_section(text, "## New Section\n\nNew content\n")
    assert replaced is False
    result = str(text)
    assert result.endswith("## New Section\n\nNew content\n")
    # Should have blank line separator before new section
    assert "\n\n## New Section" in result


def test_replace_section_append_to_empty():
    text = _make_text()
    replaced = document.replace_section(text, "## First\n\nContent\n")
    assert replaced is False
    assert str(text) == "## First\n\nContent\n"


def test_replace_section_last_section():
    text = _make_text("## A\n\nBody A\n\n## B\n\nBody B\n")
    replaced = document.replace_section(text, "## B\n\nNew B content\n")
    assert replaced is True
    result = str(text)
    assert "Body A" in result
    assert "New B content" in result
    assert "Body B" not in result


def test_replace_section_preserves_subsections_of_peer():
    text = _make_text("## A\n\nBody A\n\n### A.1\n\nNested\n\n## B\n\nBody B\n")
    replaced = document.replace_section(text, "## A\n\nReplaced A\n")
    assert replaced is True
    result = str(text)
    assert "Replaced A" in result
    assert "Body A" not in result
    assert "Nested" not in result  # subsection was part of ## A
    assert "## B\n\nBody B" in result


def test_replace_section_content_without_trailing_newline():
    text = _make_text("## A\n\nOld\n\n## B\n\nBody B\n")
    replaced = document.replace_section(text, "## A\n\nNew content")
    assert replaced is True
    result = str(text)
    # Should still work even without trailing newline
    assert "New content" in result
    assert "## B" in result
