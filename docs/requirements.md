---
date: 2026-03-28
topic: tafelmusik
---

# Tafelmusik — Collaborative Editing Between Claude and Sameer

## Problem Frame

Sameer and Claude co-author documents regularly — reports, plans, briefs, strategy narratives. The current workflows are functional but clunky:

1. **Sublime co-edit** — Claude edits a markdown file on hezza, Sameer edits in Sublime on Mac. Files shuffle via Taildrive, which is outside Claude's CWD and creates friction. No comments, no change awareness.
2. **Mise + Google Docs** — Claude creates a doc via mise, Sameer edits in the browser, Claude pulls changes back to markdown. Round-tripping works but isn't always clean. Images require temporarily hosting on a public URL. No shared editing surface — it's a relay, not a collaboration.

Both workflows are turn-based by necessity: Claude writes, Sameer reviews, Sameer edits, Claude reads the result. Neither party can see what the other is doing between turns. Comments happen in chat, disconnected from the text they refer to.

The machine boundary compounds this — Claude typically runs on hezza (Hetzner cloud), Sameer works on a MacBook Air. Files need to cross that gap, and the crossing is where things break.

## Requirements

**Shared Editing Surface**

- R1. A Yjs-based collaboration layer hosted on hezza, with a web editor as the default client
- R2. Both Sameer (via any editor — web, Sublime, iA Writer) and Claude (via pycrdt MCP) can edit the same Y.Text document
- R3. Claude is notified of changes via Channels (async push), not polling
- R14. The collaboration layer is editor-agnostic — Y.Text is the source of truth, editors are pluggable clients

**Comments and Annotations**

- R4. Claude can add comments anchored to specific text in the document
- R5. Sameer can add comments that Claude sees when it reads the document (e.g., "make this punchier," "rewrite this section")
- R6. Comments are visible in-editor alongside the text, not in a separate channel

**Images and Rich Content**

- R7. Drag-and-drop image upload from Sameer's machine, stored on hezza
- R8. Uploaded images do not enter Claude's conversation context — file deposit pattern, not inline injection
- R9. Tables render in preview (markdown pipe syntax in source)

**Export**

- R10. Clean export to Google Docs via mise, preserving headings, tables, lists, bold/italic, links, blockquotes, and code blocks
- R11. Images hosted on hezza are insertable into Google Docs via mise's upload-share-insert-revoke pattern (Tailscale URLs are not reachable from Google's servers)
- R12. Markdown is the interchange format — the document can be exported as clean markdown at any point

**Naming and Modularity**

- R13. "Tafelmusik" is the codename. The production name is TBD. The codebase should use the codename in a single, easily replaceable location (not scattered through strings and paths)

## Success Criteria

- Co-editing a document feels materially less clunky than the current Sublime/Taildrive or mise/Google Docs loops
- Sameer can leave a comment like "expand this section" and Claude can see it and act on it without a chat message
- Claude can detect that Sameer reordered sections or added a paragraph without being told
- Export to Google Docs produces a document that looks professional — no formatting artefacts, tables intact, images present
- Images uploaded during editing don't pollute Claude's conversation context

## Scope Boundaries

- Not a real-time keystroke collaboration tool — awareness of changes between turns is sufficient
- Not a replacement for Google Docs — Docs is the delivery format for sharing with others; Tafelmusik is the authoring surface
- Not multi-user beyond Sameer + Claude (no team collaboration, no auth system)
- Not a general-purpose editor or CMS — optimised for the specific co-editing workflow between one human and one AI
- No mobile support required
- No offline support required

## Key Decisions

- **Y.Text over Y.XmlFragment (BlockNote)**: BlockNote was validated (clean round-trip, good UX) but uses XmlFragment internally, which pycrdt cannot safely write to — wrong structure corrupts the editor. Y.Text is a plain string. pycrdt reads and writes it safely. Claude becomes a true Yjs peer, not an HTTP client. This eliminates the need for Hocuspocus, Express API routes, and ServerBlockNoteEditor.
- **CodeMirror 6 as the default web editor**: Tested alongside EasyMDE and BlockNote. Sameer preferred the visual feel, split preview, and code folding. Raw markdown editing is acceptable — Sameer already edits markdown in Sublime.
- **Editor-agnostic architecture**: Y.Text is the source of truth. The web editor (CodeMirror) is one client. Sublime (via rsubl + file↔Y.Text bridge) and iA Writer (via file sync) are future clients. The collaboration layer doesn't assume a specific editor.
- **~~Single Python process~~ Three processes (superseded by plan)**: The ASGI server must outlive CC sessions, so MCP and channel servers are separate ephemeral processes that connect as Yjs clients via WebSocket. See plan's Key Technical Decisions for rationale. pycrdt-websocket still provides the ASGI server. No Node.js on the server.
- **Channels for async notification**: Claude Code's `--dangerously-load-development-channels` allows MCP servers to push notifications directly into the session. The MCP server observes Y.Text changes via pycrdt and pushes "Sameer edited section 3" as a channel notification. No polling, no hooks.
- **Comments via Y.Map + StickyIndex**: Comments stored in a Y.Map alongside Y.Text. Each comment anchored via pycrdt's StickyIndex — position-tracked anchors that survive concurrent edits. Both Claude (pycrdt) and the web editor (JS) read/write the same Y.Map.
- **Mise for Google Docs export**: Validated. Y.Text content is already markdown. Claude reads it, writes to a mise deposit folder, calls mise `do(create)`.
- **iA Writer annotation export (future)**: When provenance matters, export with `@Human`/`&AI`/`*Reference` annotations in iA Writer's Markdown Annotations format.
- **File deposit pattern for images**: Borrowed from Gueridon. Uploaded images land on disk, served via URL. Claude sees them as markdown image references, not injected content.

## Dependencies / Assumptions

- hezza is available and accessible via Tailscale (already the case)
- pycrdt (0.12.50) and pycrdt-websocket (0.16.0, now `pycrdt.websocket`) are mature enough for production use (used by JupyterLab, official y-crdt org, Rust core)
- pycrdt-websocket includes ASGIServer + SQLiteYStore — no separate Yjs server needed
- Claude Code Channels (`--dangerously-load-development-channels`) works for push notifications (research preview, battle-tested in aboyeur)
- Mise's Google Docs import handles markdown faithfully (validated with test document 2026-03-28)
- pycrdt's StickyIndex provides position-tracked anchors for comments (JSON-serializable, 8 bytes each)

## Outstanding Questions

### Resolved During Exploration

- **Claude connection method**: ~~Single Python process~~ Three-process architecture (superseded by plan). MCP server is a separate ephemeral process connecting to the ASGI server as a pycrdt Yjs client via WebSocket. See plan for rationale.
- **Google Docs export**: Manual via Claude + mise. Claude reads `str(text)`, writes deposit, calls mise.
- **Image storage**: Persistent at `data/uploads/`. No cleanup policy for v1.
- **Loading existing docs**: Yes — write markdown content to Y.Text via MCP tool.
- **Project location**: `~/Repos/batterie/tafelmusik/` — standard batterie structure.
- **Editor choice**: CodeMirror 6 (web) as default. Editor-agnostic architecture supports future Sublime/iA Writer clients via file↔Y.Text bridge.
- **Async notification**: Channels (Claude Code research preview). MCP server observes Y.Text, pushes channel notifications on change.

### Deferred to Implementation

- Comment UX details: how to render Y.Map comments as CodeMirror decorations (underlines, margin markers, popover)
- File↔Y.Text bridge for Sublime/iA Writer (future unit, wiring laid in v1)
- iA Writer annotation export format (future)
- Ctrl+G → Sublime "empty terminal on return" bug (existing issue, not Tafelmusik-specific)

## Next Steps

→ Plan updated at `docs/plans/2026-03-28-001-feat-tafelmusik-collaborative-editor-plan.md`. Pick up with `/ce:work` in a fresh session.
