# Editing Grammar

Living design doc. Updated as we encounter real editing scenarios in collaborative documents.

## Principle

The editing tools should match how Claude and Sameer naturally want to edit, not force either party to think about CRDT internals. Every scenario below came from real use — if a pattern keeps appearing, it needs a first-class operation.

## Current tools (v0.1)

| Mode | Scope | Risk |
|------|-------|------|
| `replace_all` | Entire document | Safe but nuclear |
| `append` | End of document | Safe |
| `replace_section` | Heading → next same-or-higher heading | h1 swallows all; duplicate headings collide; exact match required; strips authorship on replaced range |

## Scenarios observed

### 1. Comment-driven rewrite (imprecise anchor)

**Situation:** Sameer adds a comment like "this section needs to be more concise" anchored roughly to a paragraph. Claude needs to rewrite nearby text.

**Current approach:** `replace_section` on the containing heading. Works if the section is small. Dangerous if the section is large — stomps concurrent edits in the same section.

**What's needed:** Replace a specific paragraph or range within a section without touching the rest.

**First seen:** 2026-03-30, wombles editing session (hypothesised from comment design).

---

### 2. Typo fix in someone else's edits

**Situation:** Sameer made inline tweaks. Claude spots "the the" or a repeated word. Needs a 3-character surgical fix.

**Current approach:** `replace_section` replaces hundreds of characters to change 3. Also strips Sameer's authorship attrs since `document.py` re-inserts with `author=claude`.

**What's needed:** Find-and-replace on a text snippet. Touch only the matched characters. Preserve authorship on untouched text.

**First seen:** 2026-03-30, close discussion.

---

### 3. Reorder and fix a list

**Situation:** Sameer says "reorder these and fix the markdown list numbering." A structural edit within a section.

**Current approach:** `replace_section` — all-or-nothing replacement. Works but heavy-handed.

**What's needed:** Replace a contiguous block of text (the list) without affecting surrounding content in the same section.

**First seen:** 2026-03-30, close discussion.

---

### 4. h1 section replace destroys document

**Situation:** Claude uses `replace_section` on a `# Title` heading. Since there's no equal-or-higher heading, the section boundary extends to EOF. Everything below the title is replaced.

**Current approach:** `replace_all` as a recovery. Or carefully include all lower sections in the replacement content.

**What's needed:** Either refuse h1 replace (force `replace_all`), or require explicit confirmation that the intent is to replace everything.

**First seen:** 2026-03-30, wombles Characters section lost when replacing `# Underground, overground`.

---

### 5. Concurrent editing in same section

**Situation:** Sameer is editing paragraph 3 of a section while Claude replaces paragraph 1 via `replace_section`. Claude's replace overwrites Sameer's in-flight edits because `replace_section` deletes the entire range.

**Current approach:** No mitigation. Whoever writes last wins for the replaced range.

**What's needed:** Operations that target sub-section ranges so non-overlapping edits coexist.

**First seen:** 2026-03-30, inferred from architecture (not yet observed in practice).

---

## Proposed direction: `patch` mode

A content-addressed find-and-replace that operates on text snippets, not headings:

```
edit_doc(room, mode="patch", find="old text with context", replace="new text")
```

Properties:
- **Content-addressed:** matches by text, not position — CRDT-safe
- **Minimal blast radius:** only the matched range is touched
- **Authorship-preserving:** untouched text keeps its attrs
- **Unambiguous:** requires enough context in `find` to match exactly once (same as Claude Code's Edit tool)
- **Fails loudly:** no match → error, multiple matches → error

Open questions:
- Should `find` support regex? (Probably not — keep it literal for predictability)
- How much context should `find` require? (Enough to be unique — caller's responsibility)
- Should there be a `delete` variant? (`find` with empty `replace`)
- How does this interact with authorship? (New text gets `author=claude`, deleted text loses its author — same as today but scoped)

## Scenario log

Add new scenarios as they emerge from real editing sessions. Format:

```
### N. Short title

**Situation:** What happened
**Current approach:** How we handled it
**What's needed:** The ideal operation
**First seen:** Date, context
```
