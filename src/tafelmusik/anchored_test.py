"""Tests for anchored comments — the atom."""

import pytest
from tafelmusik.anchored import (
    AnchorResult,
    Comment,
    CommentStore,
    anchor,
    capture_context,
)


# --- Anchoring algorithm ---


class TestAnchor:
    def test_exact_unique_match(self):
        text = "The server handles errors gracefully and logs them."
        result = anchor(text, "handles errors gracefully")
        assert result is not None
        assert result.start == 11
        assert result.end == 11 + len("handles errors gracefully")
        assert result.confident is True

    def test_no_match(self):
        text = "The server handles errors gracefully."
        result = anchor(text, "this text does not appear")
        assert result is None

    def test_multiple_matches_disambiguated_by_prefix(self):
        text = "The cat sat. The dog sat. The bird sat."
        result = anchor(text, "sat", prefix="dog ")
        assert result is not None
        assert result.confident is True
        # Should find the "sat" after "dog"
        assert text[result.start : result.end] == "sat"
        assert result.start == text.index("dog sat") + 4

    def test_multiple_matches_disambiguated_by_suffix(self):
        text = "The cat sat. The dog sat. The bird sat."
        result = anchor(text, "sat", suffix=". The bird")
        assert result is not None
        assert result.confident is True
        assert result.start == text.index("dog sat") + 4

    def test_multiple_matches_no_context_takes_first(self):
        text = "yes and yes and yes"
        result = anchor(text, "yes")
        assert result is not None
        assert result.start == 0
        assert result.confident is False  # ambiguous

    def test_fuzzy_match_after_minor_edit(self):
        original_quote = "The server handles errors gracefully"
        edited_text = "The server now handles all errors very gracefully"
        result = anchor(edited_text, original_quote)
        assert result is not None
        assert result.confident is False
        # Should find something close
        found = edited_text[result.start : result.end]
        assert "server" in found or "handles" in found or "gracefully" in found

    def test_empty_quote_returns_none(self):
        assert anchor("some text", "") is None

    def test_empty_text_returns_none(self):
        assert anchor("", "something") is None

    def test_quote_is_entire_text(self):
        text = "hello world"
        result = anchor(text, "hello world")
        assert result is not None
        assert result.start == 0
        assert result.end == 11
        assert result.confident is True


class TestCaptureContext:
    def test_middle_of_text(self):
        text = "0123456789" * 10  # 100 chars
        prefix, suffix = capture_context(text, 40, 50)
        assert len(prefix) == 30
        assert len(suffix) == 30
        assert prefix == text[10:40]
        assert suffix == text[50:80]

    def test_near_start(self):
        text = "short prefix here and then some more text after"
        prefix, suffix = capture_context(text, 5, 10)
        assert prefix == text[0:5]  # less than 30 chars available

    def test_near_end(self):
        text = "some text here"
        prefix, suffix = capture_context(text, 10, 14)
        assert suffix == ""  # nothing after


# --- Storage ---


@pytest.fixture
def store():
    with CommentStore(":memory:") as s:
        yield s


class TestCommentStore:
    def test_create_and_get(self, store):
        c = store.create(
            author="sameer",
            target="docs/architecture",
            body="This section needs work",
            quote="The server handles errors gracefully",
            prefix="error recovery. ",
            suffix=" by returning",
        )
        assert c.id.startswith("sameer-")
        assert c.author == "sameer"
        assert c.target == "docs/architecture"
        assert c.body == "This section needs work"
        assert c.quote == "The server handles errors gracefully"
        assert c.resolved is False

        fetched = store.get(c.id)
        assert fetched is not None
        assert fetched.id == c.id
        assert fetched.body == c.body

    def test_get_nonexistent(self, store):
        assert store.get("does-not-exist") is None

    def test_create_without_anchor(self, store):
        c = store.create(
            author="claude",
            target="bon:tfm-calute",
            body="This outcome might be too broad",
        )
        assert c.quote is None
        assert c.prefix is None

    def test_resolve_and_unresolve(self, store):
        c = store.create(
            author="sameer",
            target="docs/arch",
            body="Fix this",
            quote="broken thing",
        )
        assert c.resolved is False

        resolved = store.resolve(c.id)
        assert resolved.resolved is True

        unresolved = store.unresolve(c.id)
        assert unresolved.resolved is False

    def test_list_for_target(self, store):
        store.create(author="sameer", target="doc/a", body="first", quote="x")
        store.create(author="claude", target="doc/a", body="second", quote="y")
        store.create(author="sameer", target="doc/b", body="other doc", quote="z")

        comments = store.list_for_target("doc/a")
        assert len(comments) == 2
        assert comments[0].body == "first"
        assert comments[1].body == "second"

    def test_list_excludes_resolved_by_default(self, store):
        c1 = store.create(author="sameer", target="doc/a", body="open")
        c2 = store.create(author="claude", target="doc/a", body="resolved")
        store.resolve(c2.id)

        active = store.list_for_target("doc/a")
        assert len(active) == 1
        assert active[0].body == "open"

        all_comments = store.list_for_target("doc/a", include_resolved=True)
        assert len(all_comments) == 2

    def test_threading(self, store):
        root = store.create(
            author="sameer",
            target="doc/a",
            body="What about error handling?",
            quote="handles errors",
        )
        reply1 = store.create(
            author="claude",
            target="doc/a",
            body="Good point, I'll add try/except",
            replies_to=root.id,
        )
        reply2 = store.create(
            author="sameer",
            target="doc/a",
            body="Thanks, also check the edge case",
            replies_to=reply1.id,
        )

        thread = store.list_thread(root.id)
        assert len(thread) == 3
        assert thread[0].id == root.id
        assert thread[1].id == reply1.id
        assert thread[2].id == reply2.id

    def test_thread_of_nonexistent_comment(self, store):
        assert store.list_thread("nope") == []


# --- Re-anchoring ---


class TestReanchor:
    def test_all_comments_still_match(self, store):
        text = "The server handles errors gracefully. It also logs warnings."
        store.create(
            author="sameer",
            target="doc/a",
            body="good",
            quote="handles errors gracefully",
        )
        store.create(
            author="claude",
            target="doc/a",
            body="also good",
            quote="logs warnings",
        )

        results = store.reanchor_all("doc/a", text)
        assert all(v == "anchored" for v in results.values())

    def test_deleted_text_orphans_comment(self, store):
        c = store.create(
            author="sameer",
            target="doc/a",
            body="nice",
            quote="this exact text will be removed entirely",
        )

        new_text = "Something completely different now."
        results = store.reanchor_all("doc/a", new_text)
        assert results[c.id] == "orphaned"

    def test_minor_edit_drifts_comment(self, store):
        c = store.create(
            author="sameer",
            target="doc/a",
            body="hmm",
            quote="The server handles errors gracefully",
        )

        # Small edit to the quoted text
        edited = "The server now handles most errors quite gracefully"
        results = store.reanchor_all("doc/a", edited)
        assert results[c.id] in ("drifted", "anchored")

    def test_unanchored_comments_reported(self, store):
        c = store.create(
            author="claude",
            target="doc/a",
            body="general thought about this doc",
        )

        results = store.reanchor_all("doc/a", "any text")
        assert results[c.id] == "unanchored"

    def test_context_updated_after_reanchor(self, store):
        text = "alpha beta gamma delta epsilon zeta"
        c = store.create(
            author="sameer",
            target="doc/a",
            body="note",
            quote="gamma",
            prefix="beta ",
            suffix=" delta",
        )

        # Text changes around the quote but quote stays
        new_text = "CHANGED beta gamma delta CHANGED"
        store.reanchor_all("doc/a", new_text)

        updated = store.get(c.id)
        assert updated.prefix.endswith("beta ")
        assert updated.suffix.startswith(" delta")

    def test_context_recovery_when_quote_deleted(self, store):
        """Strategy 4: quote gone but prefix/suffix still present."""
        text = "Alpha beta gamma delta epsilon zeta eta theta"
        c = store.create(
            author="sameer",
            target="doc/a",
            body="commenting on gamma",
            quote="gamma",
            prefix="beta ",
            suffix=" delta",
        )

        # Delete "gamma" but keep surrounding text
        new_text = "Alpha beta REPLACEMENT delta epsilon zeta eta theta"
        results = store.reanchor_all("doc/a", new_text)
        assert results[c.id] == "drifted"

        # The comment should now point at the region between prefix and suffix
        updated = store.get(c.id)
        assert "REPLACEMENT" in updated.quote

    def test_fully_deleted_region_orphans(self, store):
        """When both quote AND context are gone, comment is orphaned."""
        c = store.create(
            author="sameer",
            target="doc/a",
            body="gone",
            quote="unique phrase nowhere else",
            prefix="also unique prefix ",
            suffix=" also unique suffix",
        )

        new_text = "Completely different document now."
        results = store.reanchor_all("doc/a", new_text)
        assert results[c.id] == "orphaned"

    def test_resolved_comments_not_reanchored(self, store):
        c = store.create(
            author="sameer",
            target="doc/a",
            body="done",
            quote="find me",
        )
        store.resolve(c.id)

        results = store.reanchor_all("doc/a", "find me in this text")
        assert c.id not in results  # resolved comments excluded
