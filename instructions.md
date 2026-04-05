# Tafelmusik — Instruction Shard

Auto-loaded via `~/.claude/rules/tafelmusik.md`.

## Skill Loading

When `mcp__tafelmusik__` tools are available, invoke `Skill(collaborate)` before using them — it has the editing workflow context.

## Overrides

| Your Default | What I Need |
|-------------|-------------|
| Read/Write for shared documents | `mcp__tafelmusik__` tools — edits merge via CRDT, no conflicts |
| `replace_section` for small fixes | `patch` mode — minimal blast radius, preserves authorship |
| Edit without checking state | `inspect_doc` first if you haven't seen the doc recently |

## Things Worth Knowing

- **Load before editing.** `load_doc` connects to the room. Without it, edits go nowhere.
- **Avoid `replace_section` on h1 headings.** It extends to EOF and will replace the entire document from that heading down. Use `replace_all` instead.
- **Flush at natural endpoints.** `flush_doc` commits text to disk. Comments (SQLite) survive flush — flush when the text is ready, not to clean up comments.
