# Tafelmusik

Collaborative editing layer — Yjs Y.Text + CodeMirror 6 + pycrdt. Both Sameer and Claude edit the same CRDT document as Yjs peers.

## Architecture

Two processes, one codebase:

1. **ASGI server** (`asgi_server.py`) — always-running (systemd). Owns room management (Room + RoomManager), serves web editor, handles WebSocket sync. Zero pycrdt.websocket dependency — uses public pycrdt APIs only.
2. **MCP server** (`mcp_server.py`) — ephemeral (spawned by CC). Connects to ASGI server via WebSocket as pycrdt client. Provides tools (`edit_doc`, `load_doc`, `list_docs`, `flush_doc`) AND channel notifications (push alerts when Sameer edits). **No `read_doc` tool** — Claude receives document content exclusively via push (see Content Delivery below). The observer, debouncer, and notification sender all live here because transaction origins are local to a Doc instance — they don't survive the wire, so filtering Claude's own edits requires the observer to be in the same process as the Doc.

## Module Layout

```
src/tafelmusik/
  asgi_server.py       # ASGI entry point
  mcp_server.py        # MCP entry point (tools + channel notifications)
  document.py          # Y.Text operations
  authors.py           # Identity constants (CLAUDE, SAMEER, TEST)
  comments.py          # Y.Map comment operations + StickyIndex re-anchoring
  uploads.py           # Image upload handling
  *_test.py            # Tests adjacent to source
docs/                  # Markdown files on disk (calute) — room names map to file paths
public/
  index.html           # CodeMirror editor
  cm-entry.js          # JS source (unbundled)
  editor.js            # esbuild bundle (committed)
```

## Development

```bash
uv sync                                     # Install deps
uv run pytest src/                           # Run tests
uv run ruff check src/                       # Lint
uv run ruff format src/                      # Format
cd public && npx esbuild cm-entry.js --bundle --outfile=editor.js --minify  # Rebuild JS
```

## Deployment

Push-to-deploy on hezza. Three event-driven hooks in `deploy/`, all calling shared `restart-if-changed.sh`:

| Hook | Trigger | When |
|------|---------|------|
| `post-commit` | Local commit on hezza | Claude session commits code |
| `post-merge` | `git pull` merges changes | Manual or automated pull |
| `post-receive` | Push from Mac via `git push hezza main` | Cross-machine deploy |

All filter for non-test `src/tafelmusik/*.py` changes and health-check after restart. After re-cloning:
```bash
for hook in post-commit post-merge post-receive; do
    ln -sf ../../deploy/$hook.sh .git/hooks/$hook
done
git config receive.denyCurrentBranch updateInstead  # accept pushes
```
Mac setup (one-time): `git remote add hezza modha@hezza:Repos/batterie/tafelmusik`

## Key Conventions

- **Private APIs:** When using library internals (underscore-prefixed), add a comment block naming the private APIs, the validated version, and a runtime assertion. File a bon to own the functionality via public APIs. Don't block on it — ship first, own later.
- **Port:** 3456 (ASGI server). MCP server discovers via `TAFELMUSIK_URL` env var.
- **URL routing:** URL path IS the room name. `http://hezza:3456/batterie/tafelmusik/docs/foo` opens room `batterie/tafelmusik/docs/foo`. WebSocket at `/_ws/{room:path}`, static assets at `/static/`, all other paths serve `index.html` (SPA pattern). No `?room=` query parameter.
- **Docs directory (calute):** `create_app(docs_dir=...)` defaults to `~/Repos`, overridable via `TAFELMUSIK_DOCS_DIR` env var. Both ASGI and MCP must use the same path. Room names are file paths relative to docs_dir: room `batterie/tafelmusik/docs/foo` maps to `~/Repos/batterie/tafelmusik/docs/foo.md`. File listing skips dotdirs, node_modules, __pycache__, and other non-document directories.
- **File hydration priority:** .md file in docs_dir → SQLite store → empty Doc. File takes priority because SQLite may hold stale CRDT state from a previous session.
- **`flush_doc` tool:** Reads Y.Text, writes .md to docs_dir, wipes comments from Y.Map, git commits. The flush IS the compaction — sidesteps the pycrdt-store squashing bug for file-backed rooms.
- **Path validation:** `RoomManager._safe_doc_path()` resolves room names and rejects paths escaping docs_dir. `flush_doc` has the same check. Both use `Path.resolve()` + `is_relative_to()`.
- **Room retention:** File-backed rooms stay in memory after last client disconnects (`remove_if_empty` skips cleanup). This prevents CRDT re-hydration duplication when WebSocket reconnects — each re-hydration creates new CRDT ops that merge with the client's old state, doubling content.
- **`/api/rooms` response:** Returns `[{"name": str, "active": bool}]`. Active = room is in memory with clients. The MCP room poller only connects to active rooms to avoid opening a WebSocket per .md file.
- **Persistence:** SQLiteYStore stores updates (not documents). Squashing disabled (pycrdt-store 0.1.3 data-loss bug, see `.bon/understanding.md`). Re-enable after upstream fix ships.
- **Version:** Single source in `.claude-plugin/plugin.json`.
- **Tests:** Adjacent to source (`*_test.py`). pytest + pytest-asyncio.
- **Comments:** Y.Map "comments" in Y.Doc, each entry a nested Y.Map. Four anchor fields: `anchorStart`/`anchorEnd` (RelativePosition range, tracks live edits), `anchor` (single point, fallback), `quote` (text for re-anchoring when positions collapse). Don't remove `anchor` thinking it's redundant — it's the fallback. Frontend sorts by overlap (same conversation = chronological), decorations sort by strict position (RangeSetBuilder requirement). Browser uses `Y.createRelativePositionFromTypeIndex`, MCP uses `StickyIndex.to_json()` — both produce compatible `{item: {client, clock}, assoc}` JSON.
- **Comment re-anchoring:** `comments.py` exports `collect_affected(text, comments_map, start, end)` and `reanchor(text, comments_map, affected, search_start, search_end)`. Before a destructive edit (replace_section, replace_all, patch), collect comments whose StickyIndex positions fall within the section being replaced. After the edit, search for each comment's `quote` **only within the new section** — not the whole document. Found → rebuild anchors. Gone → set `orphaned: true`. Comments outside the blast radius are untouched (CRDT tracks them). Orphaned comments appear greyed out at the bottom of the browser pane.
- **Comments UI state machine:** Two modes — `document` (editing) and `commenting` (compose card visible). Card clicks use CM6 `Annotation` to mark programmatic selections, preventing false transitions. Submit via Cmd+Enter or Shift+Enter. See `cm-entry.js`.

## Content Delivery (push model)

Claude never pulls document content — the system pushes it via channel notifications. Three triggers:

1. **Room connect:** Full document pushed on initial sync (every room the poller discovers).
2. **Comment + high drift:** When a comment arrives and CRDT drift exceeds `DRIFT_THRESHOLD`, the full document is sent alongside the comment notification.
3. **Idle resync:** After `IDLE_TIMEOUT` seconds of no remote edits, if drift is high, the system auto-pushes the full document.

This means Claude can act from context (the comment quote + instruction is often enough), receive incremental updates via notifications, or get a full resync when the model is stale. If edits fail due to stale context, the fix is tuning `DRIFT_THRESHOLD` (tfm-rinoga), not adding a pull tool.

## Gotchas

- Neither the ASGI server nor the MCP server imports `pycrdt.websocket`. All sync protocol code uses public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`.
- `SQLiteYStore` is in `from pycrdt.store import SQLiteYStore`, NOT `pycrdt.websocket`.
- SQLiteYStore does NOT auto-restore state. Use the two-instance pattern: transient store reads via `apply_updates()`, fresh store for ongoing writes. See `_restore_ydoc()` in `asgi_server.py`.
- `SQLiteYStore.db_path` is a class variable. To set it dynamically, use `type("Store", (SQLiteYStore,), {"db_path": path})` — class bodies can't see enclosing function locals.
- `StickyIndex.new(text, idx, Assoc.AFTER)` — constructor `StickyIndex(text, idx)` doesn't work.
- `observe()` callbacks are synchronous — use `asyncio.Queue.put_nowait()` + async consumer. The observer callback in `mcp_server.py` has a try/except safety net because an unhandled exception would crash the sync loop.
- **Authorship & origins:** All writes through `document.py` must be wrapped in `doc.transaction(origin=author)` and use `text.insert(..., attrs={"author": author})`. Author constants live in `authors.py`. The MCP observer uses `txn.origin` to filter Claude's own edits — if you add a write path without the origin, Claude gets self-notifications. Origins are local to a Doc instance (not serialized over the wire), which is why the observer must live in the MCP server process, not a separate channel server.
- **`find_section` is code-block-aware.** Headings inside fenced code blocks (``` or ~~~) are ignored.
- **Channel notifications:** The MCP server declares `experimental: {"claude/channel": {}}` capability and sends `notifications/claude/channel` via `ServerSession.send_message()` (low-level escape hatch, mcp 1.26.0). The typed `send_notification()` API only accepts the closed `ServerNotification` union — custom methods must bypass it. Session is captured at initialization via `_install_session_capture` (wraps `Server._handle_message` to grab session on first MCP message — the `InitializedNotification`), with `_get_state()` as a fallback on first tool call. The comment consumer waits for session readiness (`session_ready` Event) instead of dropping comments. To receive notifications, start Claude Code with `--dangerously-load-development-channels server:tafelmusik`. **Notifications are comment-driven, not text-driven:** the Y.Map 'comments' observer fires on new comments from non-Claude authors; document-body text edits produce silence. The text observer resets the idle timer for drift-based resync but sends no notifications. This is the deliberate "comments as turn signal" design — if you add a text-change notification path, you're undoing the Phase 2 architecture.
- **Drift tracking:** `_compute_drift(conn, room)` returns bytes of CRDT updates since last snapshot. `DRIFT_THRESHOLD` (1024B) gates content pushes. Three push triggers: (a) comment + high drift = full doc alongside comment, (b) idle 30s after high drift = automatic resync, (c) room connect = always push full doc. Snapshots live on `AppState.room_snapshots` (dict[str, bytes]), lost on restart (fine — restart = fresh session). `IDLE_TIMEOUT` (30s) fires per-room `asyncio.TimerHandle` that checks drift before pushing. Disable idle timer in tests with `idle_timeout=None`.
- **`replace_section` refuses h1 headings.** Raises `ValueError` because h1 sections extend to EOF and would replace the entire document. Use `replace_all` instead. The guard is in `document.py`, not just `edit_doc`. See `docs/editing-grammar.md`.
- **`patch` mode for surgical edits.** `document.patch(text, find, replace, author=author)` does content-addressed find-and-replace. Exactly one match required (0 or 2+ raises ValueError). Only the matched range is deleted/inserted — authorship attrs on surrounding text are preserved. Available via `edit_doc(mode="patch", find=..., replace=...)`.
- **`heading_level` is a public function** in `document.py` (not `_heading_level`). Returns 1-6 for heading lines, None otherwise.
- Python MCP SDK: custom notifications require low-level `Server` API, not FastMCP.
- `aconnect_ws` uses anyio cancel scopes that are task-bound. The WebSocket and sync loop must run in the SAME asyncio Task — `_sync_task()` in `mcp_server.py` wraps both. Separating them across tasks causes `RuntimeError: Attempted to exit cancel scope in a different task`. Same applies to test fixtures — use `@asynccontextmanager` helpers so `__aenter__`/`__aexit__` run in the same Task. The `connect_peer()` test helper in `conftest.py` handles this correctly.
- The MCP server owns its sync protocol (~40 lines in `_sync_loop`/`_send_updates`/`_heartbeat`) using only public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`. No Provider subclass, no private API coupling.
- The ASGI server's Room broadcasts via `doc.events()` — one background task per room listens for Doc changes, sends `create_update_message` to all connected channels concurrently (`asyncio.gather`), and persists via `store.write()`. Room.start() waits for both DB initialization and the doc observer before returning, so serve() is safe to call immediately.
- Non-file-backed rooms are cleaned up when the last client disconnects (`remove_if_empty`). File-backed rooms stay in memory to prevent CRDT duplication. Next connection to a cleaned-up room restores from SQLite.
- **Room poller:** The MCP server polls `GET /api/rooms` every 5 seconds and connects to **active** rooms not yet in `state.rooms`. Supports both old format (string list) and new format (dict with `name`/`active`). Started automatically in the MCP lifespan.
- No `pycrdt-websocket` dependency anywhere — `pycrdt` + `pycrdt-store` only.
- `text[start:end] = new_content` does delete+insert but strips formatting attrs. Use `del text[start:end]` + `text.insert(start, content, attrs=...)` to preserve authorship. `document.py` handles this — call its functions rather than operating on Text directly.
- `text.clear()` removes all content (equivalent to `del text[:]`).
