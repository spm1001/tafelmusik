# Tafelmusik — Instruction Shard

Auto-loaded via `~/.claude/rules/tafelmusik.md`.

## Mandatory Skill Loading

**When `mcp__tafelmusik__` tools are available → invoke `Skill(collaborate)` before using them.**

## Overrides

| Your Default | What I Need |
|-------------|-------------|
| Read/Write for shared documents | `mcp__tafelmusik__` tools — edits merge via CRDT, no conflicts |
| `replace_section` for small fixes | `patch` mode — minimal blast radius, preserves authorship |
| Edit without checking state | `inspect_doc` first if you haven't seen the doc recently |

## Key Rules

- **Load before editing.** `load_doc` connects to the room. Without it, edits go nowhere.
- **Never `replace_section` on h1 headings.** It extends to EOF and destroys the document. Use `replace_all`.
- **Flush at natural endpoints.** `flush_doc` commits text to disk. Comments (SQLite) survive flush — flush when the text is ready, not to clean up comments.
