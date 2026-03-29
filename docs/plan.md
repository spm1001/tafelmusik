---
title: "feat: Tafelmusik — collaborative editing layer for Claude and Sameer"
type: feat
status: active
date: 2026-03-28
origin: docs/brainstorms/2026-03-28-tafelmusik-requirements.md
---

# feat: Tafelmusik — Collaborative Editing Layer

## Overview

Build an editor-agnostic collaboration layer on hezza where Sameer and Claude both edit a shared Yjs Y.Text document. **Three Python entry points, one codebase:** (1) an always-running ASGI server (systemd) holding the Y.Doc, serving the web editor, and handling WebSocket sync; (2) an ephemeral MCP server (spawned by CC per session) that connects as a pycrdt Yjs client; (3) an ephemeral channel server (spawned by CC) that observes the Y.Doc and pushes change notifications. CodeMirror 6 is the default web editor. Future editors (Sublime, iA Writer) connect via a file↔Y.Text bridge.

## Related Documents

- **Design doc (read first):** `docs/design.md`
- **Requirements:** `docs/requirements.md`

## Problem Frame

(see origin: docs/brainstorms/2026-03-28-tafelmusik-requirements.md)

## Requirements Trace

**Shared Editing Surface**

- R1. Yjs collaboration layer hosted on hezza, web editor as default client
- R2. Both Sameer (any editor) and Claude (pycrdt MCP) edit the same Y.Text
- R3. Claude notified of changes via Channels (async push)
- R14. Editor-agnostic — Y.Text is source of truth, editors are pluggable

**Comments and Annotations**

- R4. Claude can add comments anchored to specific text
- R5. Sameer can add comments Claude sees
- R6. Comments visible in-editor alongside text

**Images and Rich Content**

- R7. Drag-and-drop image upload, stored on hezza
- R8. Images don't enter Claude's conversation context
- R9. Tables render in preview (markdown pipe syntax in source)

**Export**

- R10. Clean export to Google Docs via mise
- R11. Images on hezza insertable into Google Docs via mise upload-share-insert-revoke pattern
- R12. Markdown interchange — Y.Text IS markdown

**Naming and Modularity**

- R13. "Tafelmusik" codename in single replaceable location

## Scope Boundaries

- v1 is CodeMirror web editor only. Sublime/iA Writer bridges are future.
- Not real-time keystroke collaboration — async awareness via channels
- Not multi-user beyond Sameer + Claude
- No mobile, no offline, no auth

## Key Technical Decisions

- **Y.Text over Y.XmlFragment**: Y.Text is a plain string. pycrdt reads (`str(text)`) and writes (`text.insert()`, `text += "..."`) safely. No corruption risk. Eliminates Hocuspocus, Express, ServerBlockNoteEditor, and the entire HTTP bridge. (see origin for BlockNote validation and rejection rationale)

- **All-Python, three entry points**: pycrdt-websocket 0.16.0 provides ASGIServer (`from pycrdt.websocket`); SQLiteYStore is in `pycrdt.store` (auto-installed dependency). The ASGI server (always-running, systemd) holds the Y.Doc, serves the web editor, and handles Yjs sync. The MCP server (ephemeral, spawned by CC) connects as a pycrdt Yjs client via WebSocket. The channel server (ephemeral, spawned by CC via `--dangerously-load-development-channels`) also connects as a Yjs client and pushes notifications. **These cannot be one process** because the ASGI server must outlive CC sessions — Sameer needs the editor available even when Claude isn't active. No Node.js on the server (esbuild needed as a build-time dev dependency for the JS bundle).

- **CodeMirror 6 as default web editor**: Tested alongside EasyMDE and BlockNote. Sameer preferred the feel. Raw markdown editing is acceptable. Split preview mode for rendered view.

- **Editor-agnostic architecture**: Y.Text is source of truth. Web editor is one client. Future file↔Y.Text bridge enables Sublime (rsubl) and iA Writer. Architecture doesn't assume a specific editor.

- **Channels for async notification**: `--dangerously-load-development-channels` (CC research preview, battle-tested in aboyeur). A separate channel server entry point connects to the ASGI server as a Yjs client, observes Y.Text changes, and pushes notifications. The notification content should be a semantic summary ("Sameer edited the Risks section — added a bullet about budget"), not raw deltas. This requires the channel server to diff the markdown before/after and describe the change.

- **Comments via Y.Map + StickyIndex**: Comments in a Y.Map alongside Y.Text. Each anchored via pycrdt's StickyIndex — position-tracked anchors that survive concurrent edits, JSON-serializable, 8 bytes each. Both pycrdt and JS read/write the same Y.Map.

- **Mise for Google Docs export**: Y.Text is already markdown. Claude reads it, writes deposit, calls mise. **Caveat: images still need the mise upload-share-insert-revoke dance.** Google's Docs API fetches images server-side — Tailscale URLs are not reachable from Google's infrastructure. The plan cannot bypass this; it's a Google API limitation.

## High-Level Technical Design

> *Directional guidance, not implementation specification.*

```
┌────────────────────────────────────────────────────────┐
│  hezza                                                  │
│                                                         │
│  ALWAYS RUNNING (systemd):                              │
│  ┌────────────────────────────────────────────────┐     │
│  │ ASGI server (Python, port 3456)                │     │
│  │ pycrdt-websocket ASGIServer + starlette        │     │
│  │ ├── WebSocket /ws  ← Yjs sync protocol         │     │
│  │ ├── GET /          ← static files (CodeMirror)  │     │
│  │ ├── GET /uploads/* ← uploaded images            │     │
│  │ ├── POST /upload   ← image upload               │     │
│  │ └── SQLiteYStore   ← persistence                │     │
│  │                                                 │     │
│  │ Y.Doc (in memory, synced to SQLite)             │     │
│  │ ├── Y.Text "content"  ← markdown               │     │
│  │ └── Y.Map  "comments" ← anchored comments      │     │
│  └─────────────┬──────────────┬────────────────────┘     │
│                │              │                           │
│     WebSocket (Yjs)    WebSocket (Yjs)                   │
│                │              │                           │
│  EPHEMERAL (spawned by CC per session):                  │
│  ┌─────────────▼──┐  ┌───────▼────────────────────┐     │
│  │ MCP server     │  │ Channel server             │     │
│  │ (Python/stdio) │  │ (Python/stdio)             │     │
│  │ pycrdt client  │  │ pycrdt client              │     │
│  │                │  │                             │     │
│  │ read_doc()     │  │ observe Y.Text changes     │     │
│  │ edit_doc()     │  │ diff → semantic summary    │     │
│  │ add_comment()  │  │ push channel notification  │     │
│  │ list_comments()│  │                             │     │
│  │ export_to_docs()│ │                             │     │
│  └───────┬────────┘  └────────────┬───────────────┘     │
│          │ stdio                  │ stdio (channels)     │
└──────────┼────────────────────────┼─────────────────────┘
           │                        │
    ┌──────▼────────────────────────▼──┐
    │ Claude Code session              │
    │ MCP tools + channel notifications│
    └──────────────────────────────────┘

Browser (CodeMirror + y-websocket) connects to ASGI server
directly via WebSocket — independent of CC session.

Future: file↔Y.Text bridge for Sublime/iA Writer
```

## Project Layout

```
tafelmusik/
  CLAUDE.md                        # Architecture, conventions, dev commands
  pyproject.toml                   # PEP 621, hatchling, uv
  .claude-plugin/
    plugin.json                    # Version source of truth, mcpServers block
  hooks/
    ensure-tafelmusik.sh           # SessionStart: check uv, .venv, ASGI server
  service/
    tafelmusik.service             # systemd unit for ASGI server
  public/
    index.html                     # CodeMirror editor
    editor.js                      # esbuild bundle (committed artifact)
    package.json                   # JS dev deps (esbuild, codemirror, yjs)
  src/
    tafelmusik/
      __init__.py                  # Version only, no re-exports
      asgi_server.py               # ASGI entry point: Y.Doc, WebSocket sync, starlette
      asgi_server_test.py
      mcp_server.py                # MCP entry point: FastMCP + pycrdt WebSocket client
      mcp_server_test.py
      channel_server.py            # Channel entry point: observe + notify
      channel_server_test.py
      document.py                  # Y.Text operations (read, edit, replace_section)
      document_test.py
      comments.py                  # Y.Map comment operations + StickyIndex
      comments_test.py
      uploads.py                   # Image upload handling
      uploads_test.py
  data/
    uploads/                       # Uploaded images (gitignored, persisted)
  docs/                            # Design decisions, field reports
  skills/
    tafelmusik/SKILL.md            # Claude Code usage guide
  .bon/
```

Design principles:
- **Three entry points, three files** — mirrors the architecture directly
- **Tests adjacent to source** — `document.py` + `document_test.py` in same directory
- **Domain logic extracted** — `document.py` and `comments.py` hold CRDT operations; entry points import and wire them
- **File names telegraph purpose** — no ambiguity about what each file does
- **Flat** — one level under `src/tafelmusik/`, no subdirectories
- **`public/` for browser, `src/` for Python** — clear boundary

## Development Conventions

- **Python:** uv. `[dependency-groups].dev` for test/lint deps. Python >=3.11.
- **Testing:** pytest + pytest-asyncio. Tests adjacent to source (`*_test.py` next to `*.py`). Run: `uv run pytest src/`.
- **Linting:** ruff (lint + format). Run before committing.
- **JS bundling:** `npx esbuild public/cm-entry.js --bundle --outfile=public/editor.js --minify`. One-time build, commit the bundle. Rebuild when editor JS changes.
- **Version:** Single source in `.claude-plugin/plugin.json`, read by hatchling via `[tool.hatch.version]`.
- **Build backend:** hatchling (batterie ecosystem standard).

## Module Extraction Sequencing

`document.py` is a Unit 2 concern, not Unit 1. In Unit 1, the ASGI server creates the Y.Doc directly (`doc = Doc(); doc["content"] = Text()`) — one line, no abstraction needed. `document.py` earns its existence when `replace_section` and section parsing arrive in Unit 2. The channel server (Unit 3) also imports from `document.py` for reading. Similarly, `comments.py` is a Unit 5 concern and `uploads.py` is a Unit 4 concern.

## Implementation Units

- [ ] **Unit 1: Python Yjs server with web editor**

**Goal:** Single Python process serving a Yjs WebSocket and CodeMirror editor. Two browser tabs can edit the same document collaboratively.

**Requirements:** R1, R14

**Dependencies:** None

**Files:**
- Create: `src/tafelmusik/asgi_server.py`
- Create: `src/tafelmusik/__init__.py`
- Create: `pyproject.toml`
- Create: `public/index.html`
- Create: `public/cm-entry.js` (CodeMirror source — unbundled)
- Create: `public/editor.js` (esbuild bundle — committed artifact)
- Create: `public/package.json` (JS dev dependencies)
- Create: `CLAUDE.md`

**Approach:**
- pycrdt-websocket ASGIServer with starlette routes
- SQLiteYStore for persistence
- Y.Text named "content" as the shared document
- CodeMirror 6 with y-codemirror.next binding in the browser
- esbuild to bundle the JS: `npx esbuild public/cm-entry.js --bundle --outfile=public/editor.js --minify`. One-time build, commit the bundle. JS deps in `public/package.json`: `codemirror`, `@codemirror/lang-markdown`, `@codemirror/state`, `@codemirror/view`, `marked`, `yjs`, `y-codemirror.next`, `y-websocket`, `esbuild`
- Port 3456

**Patterns to follow:**
- pycrdt-websocket ASGIServer examples from docs
- CodeMirror prototype pattern from editor-shootout: `basicSetup` + `markdown()` + `updateListener` for live preview, `EditorView.lineWrapping`, split/preview/edit tab modes. CSS for tables, code blocks, blockquotes in preview pane.

**Test scenarios:**
- Happy path: server starts, editor loads, type text, refresh, text persists
- Happy path: two tabs edit simultaneously — changes sync
- Edge case: server restarts, document survives (SQLite)
- Edge case: server restarts while browser is connected — browser reconnects, document state is consistent

**Verification:**
- Editor loads from Mac via Tailscale IP
- Content persists across restarts

---

- [ ] **Unit 2: MCP server (Claude as Yjs peer)**

**Goal:** Claude can read and edit the document via MCP tools. The MCP server is a separate ephemeral process (spawned by CC) that connects to the ASGI server as a pycrdt Yjs client via WebSocket.

**Requirements:** R2, R3, R12

**Dependencies:** Unit 1

**Files:**
- Create: `src/tafelmusik/mcp_server.py` (MCP server entry point — separate process)
- Create: `src/tafelmusik/document.py` (Y.Text operations — shared by MCP and channel servers)
- Modify: `pyproject.toml` (add mcp, httpx-ws deps)
- Create: `.claude-plugin/plugin.json`
- Create: `hooks/ensure-tafelmusik.sh`
- Test: `src/tafelmusik/mcp_server_test.py`
- Test: `src/tafelmusik/document_test.py`

**Approach:**
- FastMCP with stdio transport. Separate process from ASGI server — connects via WebSocket as a pycrdt Yjs client
- `read_doc(room)` → `str(text)` — reads from local Yjs replica
- `edit_doc(room, content, mode="replace"|"append"|"replace_section")` → Y.Text mutations. `replace_section` finds a heading by text match and replaces until the next heading of equal or higher level. No character offsets exposed to Claude.
- `load_doc(room, markdown)` → clear + write Y.Text from file content
- `list_docs()` → list available rooms/documents
- Claude connects as a pycrdt Yjs client to the ASGI server via WebSocket. Reads/writes the CRDT directly. Low latency after initial sync (loopback WebSocket + Yjs sync). First tool call per session pays sync cost. `httpx-ws` required as explicit dependency.

**Patterns to follow:**
- Mise MCP server: FastMCP, `@mcp.tool()`, `launch.sh`, `ensure-*.sh` hook
- Batterie plugin.json convention

**Test scenarios:**
- Happy path: `read_doc()` returns markdown matching editor content
- Happy path: `edit_doc("# New", mode="replace")` — browser shows the heading
- Integration: Claude edits, Sameer sees it. Sameer edits, Claude reads it.
- Error path: malformed input — doc not corrupted

**Verification:**
- `mcp__tafelmusik__read_doc()` works in a Claude Code session
- Edits via MCP appear in the browser without refresh

---

- [ ] **Unit 3: Channels (async change notification)**

**Goal:** Claude gets notified when Sameer edits, without polling.

**Requirements:** R3

**Dependencies:** Unit 2

**Files:**
- Create: `src/tafelmusik/channel_server.py` (channel server entry point — separate process)
- Modify: `.claude-plugin/plugin.json` (channel server config)
- Test: `src/tafelmusik/channel_server_test.py`

**Approach:**
- Observe Y.Text changes via `text.observe()` or `doc.observe()`
- On change from a non-Claude source, push a channel notification summarising what changed
- Use aboyeur's conductor-channel pattern as reference: `mcp.notification({ method: "notifications/claude/channel", params: { content, meta } })`
- Debounce rapid edits (e.g., 2-second window) to avoid flooding

**Patterns to follow:**
- `~/Repos/batterie/aboyeur/src/conductor-channel.ts` for the MCP notification pattern
- `--dangerously-load-development-channels` flag for CC session

**Test scenarios:**
- Happy path: Sameer types in browser, Claude sees a `<channel>` notification
- Happy path: Claude edits via MCP — no self-notification
- Edge case: rapid edits debounced into one notification

**Verification:**
- Claude Code session with channels enabled receives notification when browser edits the doc

---

- [ ] **Unit 4: Image upload**

**Goal:** Drag-and-drop images into the editor, stored on hezza, served via HTTP.

**Requirements:** R7, R8, R11

**Dependencies:** Unit 1

**Files:**
- Modify: `src/tafelmusik/asgi_server.py` (add upload route + static serving)
- Create: `src/tafelmusik/uploads.py` (upload handling logic)
- Modify: `public/cm-entry.js` (drop/paste handler — rebuild bundle after)
- Create: `data/uploads/.gitkeep`
- Test: `src/tafelmusik/uploads_test.py`

**Approach:**
- POST /upload accepts multipart, saves to `data/uploads/<uuid>.<ext>`
- GET /uploads/* serves files statically
- CodeMirror drop/paste handler: intercept file drops, upload, insert `![filename](url)` at cursor
- Images accessible over Tailscale for Google Docs import

**Patterns to follow:**
- Gueridon upload pattern
- Upload pattern from blocknote-demo prototype: UUID-based filenames (`<uuid>.<ext>`), MIME type mapping for common image formats, multipart boundary parsing

**Test scenarios:**
- Happy path: drag image into editor, `![](url)` inserted, image accessible via URL
- Happy path: image persists across restarts
- Edge case: non-image file upload handled gracefully

**Verification:**
- Image visible in preview pane after upload
- URL accessible from Mac

---

- [ ] **Unit 5: Comments**

**Goal:** Sameer and Claude can add, read, and resolve comments anchored to specific text.

**Requirements:** R4, R5, R6

**Dependencies:** Unit 2 (MCP tools), Unit 1 (editor)

**Files:**
- Modify: `src/tafelmusik/mcp_server.py` (add comment MCP tools)
- Modify: `src/tafelmusik/comments.py` (comment operations + StickyIndex)
- Modify: `public/cm-entry.js` (comment decorations + UI — rebuild bundle after)

**Approach:**
- Y.Map "comments" in the Y.Doc, keyed by comment ID
- Each comment: `{ anchor: StickyIndex, quote: str, author, body, resolved }` — single point anchor + quoted text, not start/end range. The anchor provides location; the quote provides resilience and is returned by `list_comments()`.
- When `replace_section` destroys an anchor, re-anchor by searching for quote text in the new content. If quote text was deleted, mark comment as orphaned with a note.
- MCP tools: `add_comment(quote, body)`, `list_comments()`, `resolve_comment(id)`
- CodeMirror renders comments as decorations: resolve anchor position, search forward for quote text, underline the match
- JS side reads Y.Map, resolves StickyIndex to current offset, locates quote text, creates decoration

**Why point + quote, not start/end ranges:** A comment anchored by two StickyIndexes (start + end) has twice the fragility surface. When `replace_section` deletes text, BOTH anchors collapse to the deletion boundary — the comment loses its range and cannot self-recover. A single point anchor + stored quote string is more resilient: the anchor provides location (disambiguates when the same text appears twice), and the quote provides content identity for re-anchoring. After a section replacement, search for the quote text — if found, create a fresh anchor; if not, the comment is genuinely orphaned. The quote also serves double duty as metadata returned by `list_comments()`.

**Execution note:** Exploratory — StickyIndex usage in practice needs hands-on validation. Start with `add_comment` and `list_comments` before building the CodeMirror decoration UI.

**Test scenarios:**
- Happy path: Claude adds comment, Sameer sees underlined text with popover
- Happy path: Sameer adds comment, `list_comments()` returns it
- Happy path: comments survive concurrent edits (StickyIndex tracks position)
- Edge case: quoted text not found — clear error

**Verification:**
- Bidirectional comment flow works between Claude and browser

---

- [ ] **Unit 6: Google Docs export**

**Goal:** One-command export of the document to Google Docs via mise.

**Requirements:** R10, R11, R12

**Dependencies:** Unit 2 (read_doc)

**Files:**
- Modify: `src/tafelmusik/mcp_server.py` (add export_to_docs tool)

**Approach:**
- `export_to_docs(title)` reads Y.Text, writes to a mise deposit folder, calls mise `do(operation="create", source=...)`.
- Images require the mise upload-share-insert-revoke pattern — Google's Docs API fetches images server-side and cannot reach Tailscale URLs. See Gotcha 3.

**Test scenarios:**
- Happy path: `export_to_docs("Q1 Report")` creates a Google Doc with correct formatting
- Happy path: images in the document appear in the Google Doc

**Verification:**
- Google Doc created with headings, tables, lists, images intact

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| pycrdt client + FastMCP coexisting in ephemeral MCP server process may have event loop conflicts | Both are asyncio-native. Test early in Unit 2. These are separate processes from the ASGI server. |
| StickyIndex anchors destroyed by `replace_section` (confirmed in spike) | Point + quote architecture: single anchor + stored quote text. Re-anchor by text search after section replacement. Orphan comments whose quote was deleted. |
| Channels API is research preview — may change | Aboyeur already depends on it. Track CC changelog. Fall back to polling tool if removed. |
| CodeMirror comment decorations are custom work | Start with minimal UI (underline + tooltip). Iterate on UX. |
| File↔Y.Text bridge (future) needs careful debouncing to avoid ping-pong | Not a v1 problem. Design the bridge when we add Sublime support. |

## Documentation / Operational Notes

- Server runs as a systemd unit (`tafelmusik.service`), following gueridon pattern
- `data/uploads/` and `tafelmusik.db` (SQLite) should be backed up
- Access URL: `http://<hezza-tailscale-ip>:3456`
- Claude accesses via MCP tools (auto-started by plugin system)
- Channels require `--dangerously-load-development-channels server:tafelmusik-channel` on the CC session

### Port Management

The ASGI server port (default 3456) is defined as a constant in `asgi_server.py` and referenced by the MCP/channel servers. Port conflicts from stale servers are common — the ensure hook handles detection and cleanup.

If a stale process holds the port: `lsof -ti :3456 | xargs kill` before restarting. The systemd unit should use `ExecStartPre` to check and clean up stale PIDs.

The MCP and channel servers discover the ASGI server address via a `TAFELMUSIK_URL` environment variable (default: `ws://127.0.0.1:3456`), set in the plugin.json `mcpServers` config. This avoids hardcoding the address in multiple files and allows override for non-standard setups.

### ensure-tafelmusik.sh Hook Behavior

The SessionStart hook runs every time a Claude Code session starts in this project. Designed for Claude's experience — failures should be clear and actionable, not silent.

```
1. Check uv installed          → FAIL: "uv not found — install via curl"
2. Check .venv exists           → if missing: run `uv sync`, report
3. Check ASGI server reachable  → curl -s http://127.0.0.1:3456/ (2s timeout)
   a. If reachable: OK (silent)
   b. If not reachable:
      i.  Check if port is held by stale process (lsof -ti :3456)
      ii. If stale: kill it, start service, report "Killed stale server, restarted"
      iii. If port free: start service (systemctl --user start tafelmusik)
      iv. Wait up to 5s for server, re-check
      v.  If still not reachable: FAIL with clear message
4. Report: "Tafelmusik server running on :3456" (hookSpecificOutput)
```

This means Claude never encounters "connection refused" from MCP tools without explanation — the hook either fixes the problem or explains it.

## Gotchas for the Implementing Claude

1. **Three processes, not one.** The ASGI server is always-running (systemd). The MCP server and channel server are ephemeral (spawned by CC). Don't try to run MCP stdio and ASGI in the same process — the ASGI server must outlive CC sessions.

2. **pycrdt-websocket import path changed.** v0.16.0 merged into pycrdt as a namespace package. Use `from pycrdt.websocket import ASGIServer, WebsocketServer, YRoom` NOT `from pycrdt_websocket import ...`. Old tutorials are wrong. **Verified in spike (2026-03-28).**

3. **Google Docs image URLs must be publicly accessible.** Google's API fetches images from its own servers. Tailscale URLs don't work. Use mise's existing upload-share-insert-revoke pattern. Don't try to bypass this.

4. **edit_doc needs section-level addressing, not character offsets.** Claude thinks in sections ("replace the Risks section"), not character positions. The `replace_section` mode should find a heading by text match and replace content until the next heading of equal/higher level. Expose this, not raw offsets.

5. **Channel notifications need structured diffs, not raw deltas.** Y.Text observe gives raw deltas (insert 12 chars at offset 847). That's useless. The channel server should cache the previous markdown, diff against new, and produce a structured summary: which section changed + the changed lines. NOT natural language prose — Claude reads diffs better than summaries. A heading-aware text diff (parse headings, identify section, show changed lines) is ~50 lines. A prose summariser ("Sameer added a bullet about budget") is 150-250 lines and less useful.

6. **Multi-document from the start.** All MCP tools take a `room` parameter. The ASGI server supports multiple Yjs rooms natively. Don't build a single-document system — retrofitting multi-doc is painful.

7. **CodeMirror JS bundle needs esbuild (Node.js dev dependency).** The server is Python but the frontend JS needs bundling. This is a build-time dependency only — `npx esbuild` once, commit the bundle. Don't try to serve unbundled ESM imports (they fail, we tested this).

8. **Comment authoring UX in CodeMirror is custom work.** The plan covers the data model (Y.Map + StickyIndex) but not how Sameer creates a comment. Minimum viable: select text → Cmd+M (or a button) → popover input → writes to Y.Map. This is a CodeMirror ViewPlugin + decoration, not a trivial feature.

9. **Spike results (2026-03-28) — what was validated:**
    - `Y.Text`: `str(text)`, `text += "..."`, `text.insert(pos, content)`, `del text[start:end]` — all work
    - `Y.Map`: `comments['c1'] = {...}`, `dict(comments['c1'])` — works, nested dicts preserved
    - `text.observe(callback)` — fires on mutation, event received
    - `StickyIndex.new(text, 12, Assoc.AFTER)` — creates anchor. `get_index()` returns current position. Tracks through inserts/deletes correctly.
    - `StickyIndex.encode()` → 8 bytes. `StickyIndex.decode(bytes, text)` round-trips.
    - `StickyIndex.to_json()` → `{"item": {"client": ..., "clock": ...}, "assoc": 0}`. `StickyIndex.from_json(dict, text)` round-trips.
    - **Caveat:** `StickyIndex(text, 12)` constructor does NOT work — must use `StickyIndex.new(text, idx, Assoc.AFTER)`. And `to_json()` may crash if internal state isn't right — prefer `encode()`/`decode()` for storage.

10. **The Ctrl+G → Sublime flow (rsubl) has a known bug.** After save-and-close, the tmux pane shows empty. This is pre-existing, not Tafelmusik-specific. Don't try to fix it here.

11. **"Tafelmusik" is a codename.** Define it in `pyproject.toml` name and `.claude-plugin/plugin.json` only. Don't scatter it through user-facing strings.

12. **WebsocketProvider renamed to Provider in pycrdt-websocket 0.16.0.** Import as `from pycrdt.websocket.yroom import Provider`. It takes a `Channel` object. Use `HttpxWebsocket` from `pycrdt.websocket.websocket` as the Channel implementation. `httpx-ws` must be added as an explicit dependency in `pyproject.toml`.

13. **`observe()` callbacks are synchronous.** pycrdt's `text.observe()` fires the callback synchronously on the mutating thread during the mutation. You cannot `await` inside them. Use `asyncio.Queue.put_nowait()` in the callback and consume from an async task for debouncing and notification sending.

14. **Python MCP SDK does not support custom notification methods via `send_notification()`.** Use `ServerSession.send_message()` with a raw `SessionMessage` wrapping `JSONRPCNotification(method="notifications/claude/channel", params={...})`. This requires capturing the session reference — use the low-level `Server` API, not FastMCP, for the channel server entry point.

15. **Starlette and ASGIServer lifespan handlers may conflict.** ASGIServer handles `websocket` scope; starlette handles `http` scope. Both have lifespan handlers. Use starlette's `Mount` to compose them, or write a custom ASGI dispatcher that routes by scope type. Test this in Unit 1 — it surfaces immediately and is easy to fix, but will block the server from starting if not handled.

16. **WebsocketServer.start() blocks forever — run as a task.** `asyncio.create_task(websocket_server.start())`, NOT `await websocket_server.start()`. The latter never returns. Without starting, client connections fail with "WebsocketServer is not running." Also: `get_room()` is a coroutine — must be awaited. (Validated in spike 2026-03-29.)

17. **HttpxWebsocket wraps an existing connection, not a URL.** The pattern is: `async with aconnect_ws(url, http_client) as ws: channel = HttpxWebsocket(ws, room_name)`. Then `Provider(doc, channel)`. httpx-ws creates the connection; HttpxWebsocket adapts it to pycrdt's Channel interface. (Validated in spike 2026-03-29.)

18. **SQLiteYStore has two path concepts.** Subclass to set `db_path` (the SQLite file location): `class TafelmusikStore(SQLiteYStore): db_path = "data/tafelmusik.db"`. Then instantiate per room: `store = TafelmusikStore(path=room_name)`. Pass the store to `YRoom(ystore=store)`.

## Sources & References

- **Origin:** [docs/brainstorms/2026-03-28-tafelmusik-requirements.md](docs/brainstorms/2026-03-28-tafelmusik-requirements.md)
- pycrdt: https://github.com/y-crdt/pycrdt
- pycrdt-websocket: https://github.com/y-crdt/pycrdt-websocket
- y-codemirror.next: https://github.com/yjs/y-codemirror.next
- Yjs: https://github.com/yjs/yjs
- CodeMirror 6: https://codemirror.net/
- Aboyeur channels: `~/Repos/batterie/aboyeur/src/conductor-channel.ts`
- Prototypes (originally in /tmp/, patterns saved inline in Unit 1 and Unit 4 approaches):
  - editor-shootout: esbuild command, CodeMirror 6 setup (basicSetup + markdown + updateListener), split/preview tabs, CSS for tables/code/blockquotes
  - blocknote-demo: upload handler (UUID filenames, MIME type mapping, multipart parsing)
  - blocknote-test: markdown round-trip validation
