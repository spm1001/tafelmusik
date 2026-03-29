"""Tests for Y.Text document operations."""

from hypothesis import given
from hypothesis.strategies import composite, integers, lists, text
from pycrdt import Doc, Text

from tafelmusik import authors, document


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
    document.replace_all(text, "New content", author=authors.TEST)
    assert str(text) == "New content"


def test_replace_all_content_to_content():
    text = _make_text("Old stuff")
    document.replace_all(text, "New stuff", author=authors.TEST)
    assert str(text) == "New stuff"


def test_replace_all_content_to_empty():
    text = _make_text("Something here")
    document.replace_all(text, "", author=authors.TEST)
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


def test_find_section_ignores_headings_in_code_blocks():
    """Headings inside fenced code blocks don't affect section boundaries."""
    content = (
        "## API\n\n"
        "```python\n"
        "# This is a comment\n"
        "def foo():\n"
        "    pass\n"
        "```\n\n"
        "## Usage\n\n"
        "Use it.\n"
    )
    start, end = document.find_section(content, "## API")
    section = content[start:end]
    # Section should include the code block — # comment is not a heading
    assert "# This is a comment" in section
    assert "def foo" in section
    # Section should stop at ## Usage
    assert "## Usage" not in section


def test_find_section_ignores_headings_in_tilde_fence():
    content = "## Notes\n\n~~~\n# shell comment\n~~~\n\n## End\n"
    start, end = document.find_section(content, "## Notes")
    section = content[start:end]
    assert "# shell comment" in section
    assert "## End" not in section


def test_find_section_unclosed_fence():
    """Unclosed fence treats rest of document as code."""
    content = "## Start\n\n```\n# not a heading\n## Also not\n"
    start, end = document.find_section(content, "## Start")
    # Everything after the fence is code — section extends to end
    assert end == len(content)


# --- replace_section ---


def test_replace_section_existing():
    text = _make_text("# Title\n\nIntro\n\n## API\n\nOld API docs\n\n## Usage\n\nUsage text\n")
    replaced = document.replace_section(text, "## API\n\nNew API docs\n", author=authors.TEST)
    assert replaced is True
    result = str(text)
    assert "New API docs" in result
    assert "Old API docs" not in result
    assert "## Usage\n\nUsage text\n" in result


def test_replace_section_append_when_missing():
    text = _make_text("# Title\n\nIntro\n")
    replaced = document.replace_section(
        text, "## New Section\n\nNew content\n", author=authors.TEST
    )
    assert replaced is False
    result = str(text)
    assert result.endswith("## New Section\n\nNew content\n")
    # Should have blank line separator before new section
    assert "\n\n## New Section" in result


def test_replace_section_append_to_empty():
    text = _make_text()
    replaced = document.replace_section(text, "## First\n\nContent\n", author=authors.TEST)
    assert replaced is False
    assert str(text) == "## First\n\nContent\n"


def test_replace_section_last_section():
    text = _make_text("## A\n\nBody A\n\n## B\n\nBody B\n")
    replaced = document.replace_section(text, "## B\n\nNew B content\n", author=authors.TEST)
    assert replaced is True
    result = str(text)
    assert "Body A" in result
    assert "New B content" in result
    assert "Body B" not in result


def test_replace_section_preserves_subsections_of_peer():
    text = _make_text("## A\n\nBody A\n\n### A.1\n\nNested\n\n## B\n\nBody B\n")
    replaced = document.replace_section(text, "## A\n\nReplaced A\n", author=authors.TEST)
    assert replaced is True
    result = str(text)
    assert "Replaced A" in result
    assert "Body A" not in result
    assert "Nested" not in result  # subsection was part of ## A
    assert "## B\n\nBody B" in result


def test_replace_section_content_without_trailing_newline():
    text = _make_text("## A\n\nOld\n\n## B\n\nBody B\n")
    replaced = document.replace_section(text, "## A\n\nNew content", author=authors.TEST)
    assert replaced is True
    result = str(text)
    # Should still work even without trailing newline
    assert "New content" in result
    assert "## B" in result


def test_replace_section_append_doc_no_trailing_newline():
    """Appending to a doc that doesn't end with a newline adds separator."""
    text = _make_text("# Title\n\nIntro")  # no trailing newline
    replaced = document.replace_section(text, "## New\n\nContent\n", author=authors.TEST)
    assert replaced is False
    result = str(text)
    assert "\n\n## New" in result


def test_replace_section_append_doc_single_trailing_newline():
    """Appending to a doc ending with one newline adds one more."""
    text = _make_text("# Title\n\nIntro\n")  # single trailing newline
    replaced = document.replace_section(text, "## New\n\nContent\n", author=authors.TEST)
    assert replaced is False
    result = str(text)
    assert "Intro\n\n## New" in result


def test_replace_section_append_doc_double_trailing_newline():
    """Appending to a doc already ending with \\n\\n adds no extra whitespace."""
    text = _make_text("# Title\n\nIntro\n\n")  # already double newline
    replaced = document.replace_section(text, "## New\n\nContent\n", author=authors.TEST)
    assert replaced is False
    result = str(text)
    assert "Intro\n\n## New" in result


# --- Property-based tests (Hypothesis) ---

# Body text alphabet — no # so lines can't be mistaken for headings
_BODY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789 .,!?"


@composite
def md_body(draw):
    """Generate body text that can't be confused for a heading."""
    lines = draw(lists(
        text(alphabet=_BODY_CHARS, min_size=0, max_size=40),
        min_size=0,
        max_size=3,
    ))
    return "\n".join(lines)


def _build_section(heading: str, body: str) -> str:
    """Build a markdown section from heading line and body text."""
    if body.strip():
        return heading + "\n\n" + body + "\n"
    return heading + "\n"


@given(
    level=integers(min_value=1, max_value=4),
    new_body=md_body(),
    existing_body=md_body(),
)
def test_prop_heading_preserved_after_replace(level, new_body, existing_body):
    """After replace_section, the target heading always appears in the result."""
    heading = "#" * level + " Target"
    existing = _build_section(heading, existing_body)
    new_content = _build_section(heading, new_body)

    text = _make_text(existing)
    document.replace_section(text, new_content, author=authors.TEST)
    assert heading in str(text)


@given(
    level=integers(min_value=1, max_value=4),
    body_a=md_body(),
    body_b=md_body(),
    body_c=md_body(),
)
def test_prop_replace_preserves_other_sections(level, body_a, body_b, body_c):
    """Replacing one section does not disturb other sections at the same level."""
    prefix = "#" * level
    sections = [
        _build_section(f"{prefix} Alpha", body_a),
        _build_section(f"{prefix} Beta", body_b),
        _build_section(f"{prefix} Gamma", body_c),
    ]
    doc = "\n".join(sections)

    text = _make_text(doc)
    document.replace_section(text, f"{prefix} Beta\n\nReplaced body\n", author=authors.TEST)
    result = str(text)

    assert f"{prefix} Alpha" in result
    assert f"{prefix} Gamma" in result
    assert "Replaced body" in result


@given(level=integers(min_value=1, max_value=4), body=md_body())
def test_prop_idempotent_double_replace(level, body):
    """Replacing a section with itself twice leaves the document unchanged."""
    heading = "#" * level + " Idem"
    section = _build_section(heading, body)
    other = "#" * level + " Other\n\nKeep me\n"
    doc = other + "\n" + section

    text = _make_text(doc)
    document.replace_section(text, section, author=authors.TEST)
    after_first = str(text)
    document.replace_section(text, section, author=authors.TEST)
    after_second = str(text)

    assert after_first == after_second


@given(level=integers(min_value=1, max_value=4), body=md_body(), new_body=md_body())
def test_prop_find_after_replace(level, body, new_body):
    """After replacing, find_section locates the section starting with the heading."""
    heading = "#" * level + " Findable"
    original = _build_section(heading, body)
    new_content = _build_section(heading, new_body)

    text = _make_text(original)
    document.replace_section(text, new_content, author=authors.TEST)
    result = str(text)

    bounds = document.find_section(result, heading)
    assert bounds is not None, f"Heading '{heading}' not found after replace"
    section_text = result[bounds[0] : bounds[1]]
    assert section_text.startswith(heading)


@given(
    parent_level=integers(min_value=1, max_value=3),
    parent_body=md_body(),
    child_body=md_body(),
    sibling_body=md_body(),
)
def test_prop_replace_subsumes_children(parent_level, parent_body, child_body, sibling_body):
    """Replacing a section also replaces its subsections (lower-level headings)."""
    child_level = parent_level + 1
    pp = "#" * parent_level
    pc = "#" * child_level

    sections = [
        _build_section(f"{pp} Parent", parent_body),
        _build_section(f"{pc} Child", child_body),
        _build_section(f"{pp} Sibling", sibling_body),
    ]
    doc = "\n".join(sections)

    text = _make_text(doc)
    document.replace_section(text, f"{pp} Parent\n\nNew parent body\n", author=authors.TEST)
    result = str(text)

    assert f"{pp} Parent" in result
    assert "New parent body" in result
    assert f"{pc} Child" not in result  # child was inside Parent's section
    assert f"{pp} Sibling" in result


@given(
    level=integers(min_value=1, max_value=4),
    existing_body=md_body(),
    new_body=md_body(),
)
def test_prop_append_creates_findable_section(level, existing_body, new_body):
    """Appending a new section (heading not found) makes it findable."""
    prefix = "#" * level
    existing = _build_section(f"{prefix} Existing", existing_body)
    new_heading = f"{prefix} Appended"
    new_section = _build_section(new_heading, new_body)

    text = _make_text(existing)
    replaced = document.replace_section(text, new_section, author=authors.TEST)
    assert replaced is False  # appended, not replaced

    bounds = document.find_section(str(text), new_heading)
    assert bounds is not None, f"Appended heading '{new_heading}' not findable"
