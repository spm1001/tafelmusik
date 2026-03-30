# Editing Grammar

Living design doc. Updated as we encounter real editing scenarios in collaborative documents.

## Principle

The editing tools should match how Claude and Sameer naturally want to edit, not force either party to think about CRDT internals. Every scenario below came from real use — if a pattern keeps appearing, it needs a first-class operation.

## Current tools (v0.2)

| Mode | Scope | Risk |
|------|-------|------|
| `replace_all` | Entire document | Safe but nuclear |
| `append` | End of document | Safe |
| `replace_section` | Heading → next same-or-higher heading | h1 refused (use replace_all); duplicate headings collide; exact match required; strips authorship on replaced range |
| `patch` | Literal text match → replace | Minimal blast radius; preserves authorship on untouched text; fails on 0 or 2+ matches |

## Scenarios observed

### 1. Comment-driven rewrite (imprecise anchor)

**Situation:** Sameer adds a comment like "this section needs to be more concise" anchored roughly to a paragraph. Claude needs to rewrite nearby text.

**Current approach:** `replace_section` on the containing heading. Works if the section is small. Dangerous if the section is large — stomps concurrent edits in the same section.

**What's needed:** Replace a specific paragraph or range within a section without touching the rest.

**Solution:** `edit_doc(mode="patch", find="paragraph text with enough context", replace="rewritten paragraph")`. Targets the exact paragraph — surrounding content untouched. Test: `test_scenario_1_comment_driven_rewrite`.

**First seen:** 2026-03-30, wombles editing session (hypothesised from comment design).

---

### 2. Typo fix in someone else's edits

**Situation:** Sameer made inline tweaks. Claude spots "the the" or a repeated word. Needs a 3-character surgical fix.

**Current approach:** `replace_section` replaces hundreds of characters to change 3. Also strips Sameer's authorship attrs since `document.py` re-inserts with `author=claude`.

**What's needed:** Find-and-replace on a text snippet. Touch only the matched characters. Preserve authorship on untouched text.

**Solution:** `edit_doc(mode="patch", find="the the", replace="the")`. Only the matched range is deleted and re-inserted — surrounding authorship attrs preserved. Test: `test_scenario_2_typo_fix_preserves_authorship`.

**First seen:** 2026-03-30, close discussion.

---

### 3. Reorder and fix a list

**Situation:** Sameer says "reorder these and fix the markdown list numbering." A structural edit within a section.

**Current approach:** `replace_section` — all-or-nothing replacement. Works but heavy-handed.

**What's needed:** Replace a contiguous block of text (the list) without affecting surrounding content in the same section.

**Solution:** `edit_doc(mode="patch", find="1. Third task\n2. First task\n3. Second task", replace="1. First task\n2. Second task\n3. Third task")`. Multiline patch finds the list block and replaces it. Test: `test_scenario_3_reorder_list`.

**First seen:** 2026-03-30, close discussion.

---

### 4. h1 section replace destroys document

**Situation:** Claude uses `replace_section` on a `# Title` heading. Since there's no equal-or-higher heading, the section boundary extends to EOF. Everything below the title is replaced.

**Current approach:** `replace_all` as a recovery. Or carefully include all lower sections in the replacement content.

**What's needed:** Either refuse h1 replace (force `replace_all`), or require explicit confirmation that the intent is to replace everything.

**Solution:** `edit_doc(mode="replace_section")` now refuses h1 headings with a clear error message directing to `mode="replace_all"`. Test: `test_scenario_4_h1_replace_refused`.

**First seen:** 2026-03-30, wombles Characters section lost when replacing `# Underground, overground`.

---

### 5. Concurrent editing in same section

**Situation:** Sameer is editing paragraph 3 of a section while Claude replaces paragraph 1 via `replace_section`. Claude's replace overwrites Sameer's in-flight edits because `replace_section` deletes the entire range.

**Current approach:** No mitigation. Whoever writes last wins for the replaced range.

**What's needed:** Operations that target sub-section ranges so non-overlapping edits coexist.

**Solution:** `patch` mode targets specific text, not heading-bounded sections. Two patches on different paragraphs within the same section don't conflict. Test: `test_scenario_5_concurrent_non_overlapping_patches`.

**First seen:** 2026-03-30, inferred from architecture (not yet observed in practice).

---

## Implemented: `patch` mode

Content-addressed find-and-replace that operates on text snippets, not headings:

```
edit_doc(room, mode="patch", find="old text with context", replace="new text")
```

Properties:
- **Content-addressed:** matches by text, not position — CRDT-safe
- **Minimal blast radius:** only the matched range is touched
- **Authorship-preserving:** untouched text keeps its attrs
- **Unambiguous:** requires enough context in `find` to match exactly once (same as Claude Code's Edit tool)
- **Fails loudly:** no match → error, multiple matches → error
- **Delete variant:** empty `replace` string deletes the matched text

Resolved questions:
- No regex — literal matching only, for predictability
- Context is caller's responsibility — include enough to be unique
- Authorship: new text gets `author=claude`, deleted text loses its author, surrounding text untouched

## Scenario log

Add new scenarios as they emerge from real editing sessions. Format:

```
### N. Short title

**Situation:** What happened
**Current approach:** How we handled it
**What's needed:** The ideal operation
**First seen:** Date, context
```
