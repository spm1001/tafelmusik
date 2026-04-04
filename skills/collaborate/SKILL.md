---
name: collaborate
description: Orchestrates collaborative document editing. Load before any mcp__tafelmusik__ tool call. Invoke first when user says 'edit the doc', 'check the document', 'add a comment', 'write together', or 'collaborate on this' — teaches load-first workflow, edit mode selection, and comment conventions that prevent data loss.
---

# Collaborate

Tafelmusik is a shared CRDT document where you and Sameer co-edit markdown in real time. Sameer sees a CodeMirror editor in the browser; you operate via MCP tools. Edits merge automatically — no conflicts, no locking.

**Iron law: load before editing.** `load_doc` connects you to the room. Without it, your edits go nowhere.

## Workflow

### 1. Find or create a document

```
list_docs               → shows available documents (name + active status)
load_doc(room, markdown) → creates/overwrites a document with initial content
```

Room names are file paths: `batterie/tafelmusik/docs/foo` maps to `~/Repos/batterie/tafelmusik/docs/foo.md` on disk. Navigating to `hezza:3456/batterie/tafelmusik/docs/foo` in the browser opens the same room.

### 2. See what's there

```
inspect_doc(room) → full text with authorship attrs + drift score
list_comments(room) → all comments, sorted by position
```

`inspect_doc` shows who wrote what (author attrs on each chunk) and the drift score — high drift means your mental model may be stale. Use it before editing if you haven't seen the doc recently.

### 3. Edit

Four modes, from lightest to heaviest:

| Mode | When to use | Parameters |
|------|------------|------------|
| `patch` | Fix a typo, rewrite a paragraph, reorder a list | `find` + `replace` |
| `replace_section` | Rewrite an entire section (h2-h6 only) | `content` starts with heading |
| `append` | Add new content at the end | `content` |
| `replace_all` | Full document rewrite or h1 sections | `content` |

**Default to `patch`.** It has the smallest blast radius — only the matched text is touched, surrounding content and authorship are preserved. Include enough context in `find` to match exactly once.

```
edit_doc(room, mode="patch", find="the the quick", replace="the quick")
edit_doc(room, mode="replace_section", content="## Revised Section\n\nNew content here.")
edit_doc(room, mode="append", content="\n## New Section\n\nAdded at the end.")
edit_doc(room, mode="replace_all", content="# Fresh Start\n\nEntire document replaced.")
```

**Avoid `replace_section` on h1 headings** — it extends to EOF and will destroy everything below. Use `replace_all` instead.

### 4. Comment

Comments are inline reactions anchored to specific text — not editorial workflow, not code review.

```
add_comment(room, quote="exact text from doc", body="Your reaction or suggestion")
resolve_comment(room, quote="the quoted text")
```

The `quote` must be an exact substring of the document. Sameer sees the comment as a highlighted annotation in the browser. Use comments to ask questions, suggest changes, or react to specific passages.

### 5. Persist

```
flush_doc(room) → writes Y.Text to .md file, wipes comments, git commits
```

Flush is the "save" — it creates the durable .md file on disk. Comments are ephemeral session annotations and are cleared on flush. Only flush when the document is in a good state.

## When to Use

- Sameer asks you to write, edit, or review a document together
- You need to react to specific text Sameer wrote (use comments)
- Sameer says "check the doc" or "I updated the document"

## When NOT to Use

- Reading/writing local files (use Read/Write tools directly)
- One-off text generation with no collaboration intent
- Sameer hasn't mentioned a shared document

## Anti-Patterns

| Mistake | Consequence | Fix |
|---------|------------|-----|
| Edit without `load_doc` first | Edit silently fails or targets wrong room | Always load first |
| `replace_section` on `# Title` | Destroys everything below h1 | Use `replace_all` |
| `replace_all` for a typo fix | Stomps Sameer's concurrent edits | Use `patch` |
| Guessing document content | Stale edits, broken patches | `inspect_doc` first |
| Flushing mid-conversation | Wipes comments Sameer hasn't seen | Flush at natural endpoints |
