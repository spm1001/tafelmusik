---
date: 2026-03-28
topic: tafelmusik
type: design
---

# Tafelmusik — Design Document

## What this is

Tafelmusik (codename) is a collaborative editing layer that lets Sameer (human, MacBook) and Claude (AI, hezza server) co-author markdown documents without the friction of file shuffling, turn-taking, or format conversion.

## The problem, specifically

Sameer and Claude co-write documents regularly — reports, strategy narratives, plans, briefs. Two workflows exist today. Both work. Both are clunky.

**Workflow 1: Sublime co-edit.** Claude edits a markdown file on hezza. Sameer edits in Sublime on his Mac. Files move via Taildrive, which is outside Claude's working directory. No comments ("make this punchier" happens in chat, disconnected from the text). No change awareness (Claude doesn't know Sameer reordered section 3 until told).

**Workflow 2: mise + Google Docs.** Claude creates a Google Doc via mise. Sameer edits in the browser. Claude pulls changes back to markdown. The round-trip mostly works but images are painful (need temporary public URLs for the Docs API), and the whole thing is a relay — write, wait, pull, read, write, wait, pull. Neither party can see what the other is doing between turns.

The machine boundary makes everything worse. Claude runs on hezza (Hetzner cloud, EU). Sameer works on a MacBook Air. Both are on the same Tailscale network, but every file crossing adds friction.

## What we want

1. A shared document that both can edit without shuffling files between machines
2. Comments anchored to specific text — "expand this," "I moved this section" — visible alongside the content, not in a separate chat
3. Claude knows when Sameer has edited, without being told (async push, not polling)
4. Clean export to Google Docs when the document needs to go to other humans
5. Images that work (drag-and-drop, no "temporarily make it public" dance for the editing surface)
6. Markdown as the native format — it's what Claude thinks in and what exports to everything else

## What we explored and why we rejected it

### Proof (proofeditor.ai)
Tested live. Joined a doc, added comments, replaced content. The provenance tracking (purple = AI, green = human) is genuinely interesting. But: the API is snapshot-based (poll, don't stream), block edits required fetching snapshots and batch-deleting/inserting by block ID, and there's no path to owning the infrastructure. We'd be renting a polished version of something we can build.

### BlockNote (WYSIWYG block editor on Yjs)
Built a prototype, tested markdown round-trip (clean — cosmetic changes only), validated image upload, pushed to Google Docs via mise. The editing UX is nice (Notion-like blocks, slash commands, drag-to-reorder). But: BlockNote stores documents as Y.XmlFragment — a deeply nested XML tree. Python (pycrdt) cannot safely write to this structure. Wrong nesting corrupts the editor. This forced a split architecture: Python for reads, JS/HTTP for writes, Hocuspocus server, Express API routes, ServerBlockNoteEditor. The complexity exploded — 8 moving parts for what should be simple.

### Google Docs (fix instead of replace)
Explored adding a Drive watcher for change notifications + comment write operations in mise. The "build nothing new" argument is strong (~630 lines of Python additions to existing tools). But: Google Docs will always be a relay. No cursor presence, no real-time awareness, notifications are "file changed" not "section 3 edited." The image limitation (Google fetches images server-side, needs public URLs) is a platform constraint we can't fix. And the round-trip fidelity between markdown and Google's internal format is lossy.

### Filesystem minimalist (just edit a file on disk)
One Node.js server, one HTML file (EasyMDE), fs.watch for notifications. Radically simple. Claude uses standard Read/Edit tools — no MCP server needed. But: last-write-wins conflicts, no structured comments (only HTML comments in the markdown), and the "coordination" is social ("your turn") not technical. Fine for a scratchpad, insufficient for serious co-authoring.

### iA Writer
Their Markdown Annotations format (`@Human`, `&AI`, `*Reference`) is the best provenance tracking system we found. The URL command scheme (`ia-writer://write?mode=patch&author=Claude`) is clever. But iA Writer is Mac-local only, has no inbound events (Claude can push to it but can't know when Sameer edits), and the annotations format is designed to be software-written (editing outside iA Writer breaks the hash validation).

## What we chose and why

**Y.Text + CodeMirror 6 + pycrdt.** The key insight: if the Yjs document uses Y.Text (a plain string containing markdown) instead of Y.XmlFragment (a rich document tree), pycrdt can safely read AND write as a full Yjs peer. No corruption risk. No HTTP bridge. No conversion layer.

Claude edits markdown directly in the CRDT. The browser edits the same CRDT via CodeMirror. Both are equal Yjs peers. Conflicts are resolved by the CRDT automatically.

### Why CodeMirror over BlockNote for the editor
Sameer tested both (plus EasyMDE). CodeMirror felt right — code folding, split preview, clean visual feel. Sameer already edits markdown in Sublime; raw source editing isn't a trade-off for him. The WYSIWYG features BlockNote adds (drag-to-reorder blocks, visual table editing, slash commands) weren't important enough to justify the architectural complexity they forced.

### Why this is a collaboration layer, not an editor
The architecture doesn't assume a specific editor. Y.Text is the source of truth. CodeMirror (web) is the v1 client. But a file↔Y.Text bridge would let Sublime (via rsubl) or iA Writer connect too. Sameer picks the editor that fits the moment — browser when away from the Mac, Sublime for quick edits, iA Writer when provenance matters. The collaboration infrastructure is the same underneath.

### Why Channels for notification
Claude Code has a research preview feature (`--dangerously-load-development-channels`) that lets MCP servers push notifications directly into the conversation. Battle-tested in aboyeur (the multi-session orchestrator). This is what breaks the turn-taking model — Claude doesn't poll for changes, doesn't wait to be told. The channel server observes the Y.Doc and pushes semantic summaries ("Sameer added a bullet to 'Risks and Concerns'").

## Architecture principles

1. **Y.Text is the source of truth.** Not any editor, not a file on disk, not a Google Doc. The CRDT document is canonical. Everything else is a view or an export.

2. **Three processes, one codebase.** The ASGI server (always-running, systemd) holds the Y.Doc and serves the web editor. The MCP server (ephemeral, per CC session) connects as a Yjs client. The channel server (ephemeral, per CC session) observes and pushes notifications. All Python, all in the same repo.

3. **Claude is a Yjs peer, not an API client.** Claude speaks the Yjs sync protocol via pycrdt over WebSocket — the same protocol the browser uses. No format conversion, no bespoke API between Claude and the document. The MCP tools are thin wrappers around Y.Text operations.

4. **Editors are pluggable.** The web editor is one client. Future clients connect via the same Yjs protocol (browser WebSocket) or via a file↔Y.Text bridge (Sublime, iA Writer). Adding a new editor doesn't change the collaboration infrastructure.

5. **Comments are first-class data.** Stored in a Y.Map alongside Y.Text, anchored via StickyIndex (position-tracked anchors that survive concurrent edits). Not inline HTML comments, not a sidecar file. Both Claude and Sameer read/write the same comment data structure.

6. **Export is explicit.** Google Docs is a delivery format, not a collaboration surface. Claude reads Y.Text (already markdown), writes a mise deposit, calls mise. The editing happens in Tafelmusik; the sharing happens in Docs.

## What doesn't matter (scope boundaries)

- Real-time keystroke collaboration — async awareness is sufficient
- Mobile editing — not required
- Offline support — not required
- Multi-user beyond Sameer + Claude — no auth, no team features
- Visual table editing — markdown pipe syntax is fine
- Slash commands — not important enough to justify complexity
- Block-level drag-to-reorder — text cut/paste is acceptable
- iA Writer annotation export — future, not v1

## What we validated empirically

| Test | Result |
|------|--------|
| BlockNote markdown round-trip | Clean — cosmetic changes only (bullet style, table padding) |
| BlockNote → Google Docs via mise | Works — headings, tables, lists, code blocks all survived |
| Image upload + drag-and-drop | Works — deposit pattern, files on hezza, served via HTTP |
| EasyMDE editing experience | Functional but basic |
| CodeMirror 6 editing experience | Preferred — code folding, split preview, cleaner feel |
| BlockNote comments | Available in free tier (verified pricing page) |
| Proof API (comments, edits, presence) | Works but snapshot-based, fragile batch operations |
| Google Docs image insertion | Requires publicly accessible URLs — Tailscale URLs don't work |
| pycrdt-websocket includes ASGI server | Confirmed — no separate Yjs server process needed |
| Channels (aboyeur) | Battle-tested — async push notifications into CC sessions |

## Key risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| pycrdt-websocket ASGIServer + FastMCP event loop conflicts | High | Test in Unit 1-2. These are separate processes so the risk is actually just pycrdt client + FastMCP coexisting, which is more manageable. |
| StickyIndex API may not work as documented | Medium | Validate in Unit 5. Fall back to offset-based anchoring with re-anchoring. |
| Channels API is research preview | Medium | aboyeur depends on it. MCP Triggers & Events WG targeting end April 2026 for standardisation. Fall back to polling tool. |
| CodeMirror comment UX is custom work | Medium | Start minimal (select → shortcut → popover). Iterate. |
| Channel notifications need semantic diffs, not raw deltas | Medium | Cache previous markdown, diff, describe. This is ~50 lines but requires a markdown-aware differ. |

## Provenance — what we'd like eventually

iA Writer's three-way attribution (`@Human`, `&AI`, `*Reference`) is the gold standard for document provenance. In v1, we know who wrote what because Y.Text tracks it at the CRDT level (each character has an origin client ID). In a future version, export with iA Writer Markdown Annotations format would surface this beautifully. Not v1 scope, but the architecture doesn't prevent it.

## Related documents

- **Requirements:** `docs/brainstorms/2026-03-28-tafelmusik-requirements.md`
- **Implementation plan:** `docs/plans/2026-03-28-001-feat-tafelmusik-collaborative-editor-plan.md`
- **Prototypes:** `/tmp/blocknote-test/` (round-trip), `/tmp/blocknote-demo/` (editor), `/tmp/editor-shootout/` (comparison)
