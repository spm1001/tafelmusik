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
  anchored.py          # Comment system — SQLite + TextQuoteSelector anchoring
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
- **`flush_doc` tool:** Reads Y.Text, writes .md to docs_dir, git commits. The flush IS the compaction — sidesteps the pycrdt-store squashing bug for file-backed rooms. SQLite comments are not affected by flush.
- **Path validation:** `RoomManager._safe_doc_path()` resolves room names and rejects paths escaping docs_dir. `flush_doc` has the same check. Both use `Path.resolve()` + `is_relative_to()`.
- **Room retention:** File-backed rooms stay in memory after last client disconnects (`remove_if_empty` skips cleanup). This prevents CRDT re-hydration duplication when WebSocket reconnects — each re-hydration creates new CRDT ops that merge with the client's old state, doubling content.
- **`/api/rooms` response:** Returns `[{"name": str, "active": bool}]`. Active = room is in memory with clients. The MCP room poller only connects to active rooms to avoid opening a WebSocket per .md file. The file scan (`_scan_doc_files`) is cached with a 30s TTL — new .md files appear within 30s, active rooms appear immediately.
- **Persistence:** SQLiteYStore stores updates (not documents). Squashing disabled (pycrdt-store 0.1.3 data-loss bug, see `.bon/understanding.md`). Re-enable after upstream fix ships.
- **Version:** Single source in `.claude-plugin/plugin.json`.
- **Tests:** Adjacent to source (`*_test.py`). pytest + pytest-asyncio.
- **Comments (HTTP/SQLite):** All three surfaces — MCP tools, browser, tmux — use HTTP endpoints on the ASGI server. Comments are stored in SQLite (`CommentStore` in `anchored.py`), anchored via TextQuoteSelector (quote + prefix/suffix context). Re-anchoring is lazy — computed on read via a 4-strategy cascade (exact → disambiguate → fuzzy → context recovery), not maintained on write.
  - **Browser** fetches comments via `GET /api/rooms/{room}/comments`, creates via POST, resolves via POST `.../resolve`. Real-time updates arrive as 0x01 WebSocket broadcasts (same connection as CRDT sync). The browser intercepts 0x01 messages by wrapping `ws.onmessage` before y-websocket can misinterpret them as awareness messages — see "0x01 interception" gotcha below.
  - **Notification paths — room comments:** Browser/Claude comments → `POST /api/rooms/{room}/comments` → ASGI broadcast 0x01 → all room peers. MCP server's `_handle_comment_event` routes 0x01 events into `_comment_queue`, consumed by `_comment_consumer` for channel notifications. Claude's own HTTP comments are filtered by author check.
  - **Notification paths — session-direct comments:** Tmux comments → `POST /api/sessions/{id}/comments` → 0x01 sent to one WebSocket (looked up in `session_registry`) → MCP's `_session_comment_queue` → `_session_comment_consumer` → channel notification. No room involved. Falls back to room endpoint on 404 (session not connected). The dedicated `/_ws/_session/{session_id}` WebSocket route handles session registration — no CRDT sync, no room creation.
- **Comments UI state machine:** Two modes — `document` (editing) and `commenting` (compose card visible). Card clicks use CM6 `Annotation` to mark programmatic selections, preventing false transitions. Submit via Cmd+Enter or Shift+Enter. See `cm-entry.js`.

## Content Delivery (push model)

Claude never pulls document content — the system pushes it via channel notifications. Three triggers:

1. **Room connect:** Full document pushed on initial sync (every room the poller discovers).
2. **Comment + high drift:** When a comment arrives and CRDT drift exceeds `DRIFT_THRESHOLD`, the full document is sent alongside the comment notification.
3. **Idle resync:** After `IDLE_TIMEOUT` seconds of no remote edits, if drift is high, the system auto-pushes the full document.

This means Claude can act from context (the comment quote + instruction is often enough), receive incremental updates via notifications, or get a full resync when the model is stale. If edits fail due to stale context, the fix is tuning `DRIFT_THRESHOLD` (tfm-rinoga), not adding a pull tool.

## Gotchas

- **`sqlite3` `with` is NOT a resource manager.** `with sqlite3.connect(path) as conn:` only commits/rolls back — it does NOT close the connection. Always use `try/finally: conn.close()`. This caused a production outage: `_query_persisted_rooms` leaked one FD per `/api/rooms` call, exhausting the FD limit in ~10 minutes. Regression test: `test_api_rooms_does_not_leak_fds`.
- **Logging:** Both processes use `tafelmusik.logging_config`. Stderr gets timestamped human output, JSONL call/event logs go to `~/.local/share/tafelmusik/` (`tools.jsonl` for MCP, `server.jsonl` for ASGI). Room name is the correlation key. Merge with `cat ~/.local/share/tafelmusik/*.jsonl | jq -s 'sort_by(.ts)[]'`. `configure_logging()` is called inside `create_app()` (ASGI) and `lifespan()` (MCP) — not at import time. Tests that import asgi_server without calling `create_app()` get no logging.
- Neither the ASGI server nor the MCP server imports `pycrdt.websocket`. All sync protocol code uses public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`.
- `SQLiteYStore` is in `from pycrdt.store import SQLiteYStore`, NOT `pycrdt.websocket`.
- SQLiteYStore does NOT auto-restore state. Use the two-instance pattern: transient store reads via `apply_updates()`, fresh store for ongoing writes. See `_restore_ydoc()` in `asgi_server.py`.
- `SQLiteYStore.db_path` is a class variable. To set it dynamically, use `type("Store", (SQLiteYStore,), {"db_path": path})` — class bodies can't see enclosing function locals.
- `StickyIndex.new(text, idx, Assoc.AFTER)` — constructor `StickyIndex(text, idx)` doesn't work.
- `observe()` callbacks are synchronous — use `asyncio.Queue.put_nowait()` + async consumer. The observer callback in `mcp_server.py` has a try/except safety net because an unhandled exception would crash the sync loop.
- **Authorship & origins:** All writes through `document.py` must be wrapped in `doc.transaction(origin=author)` and use `text.insert(..., attrs={"author": author})`. Author constants live in `authors.py`. The MCP observer uses `txn.origin` to filter Claude's own edits — if you add a write path without the origin, Claude gets self-notifications. Origins are local to a Doc instance (not serialized over the wire), which is why the observer must live in the MCP server process, not a separate channel server.
- **`find_section` is code-block-aware.** Headings inside fenced code blocks (``` or ~~~) are ignored.
- **Channel notifications:** The MCP server declares `experimental: {"claude/channel": {}}` capability and sends `notifications/claude/channel` via `ServerSession.send_message()` (low-level escape hatch, mcp 1.26.0). The typed `send_notification()` API only accepts the closed `ServerNotification` union — custom methods must bypass it. Session is captured at initialization via `_install_session_capture` (wraps `Server._handle_message` to grab session on first MCP message — the `InitializedNotification`), with `_get_state()` as a fallback on first tool call. The comment consumer waits for session readiness (`session_ready` Event) instead of dropping comments. To receive notifications, start Claude Code with `--dangerously-load-development-channels server:tafelmusik` (hezza, via repo `.mcp.json`) or `--dangerously-load-development-channels plugin:tafelmusik@batterie-de-savoir` (Mac, via plugin marketplace). **Notifications are comment-driven, not text-driven:** all comments arrive via HTTP POST → ASGI 0x01 broadcast → MCP `_handle_comment_event` → queue → `_comment_consumer` → channel notification. Document-body text edits produce silence. This is the deliberate "comments as turn signal" design — if you add a text-change notification path, you're undoing the architecture.
- **0x01 WebSocket interception (browser):** The ASGI server broadcasts comment events as `0x01 + JSON` over the CRDT WebSocket. y-websocket interprets byte 0x01 as awareness protocol — the JSON payload would crash awareness parsing. The browser wraps `ws.onmessage` after y-websocket sets it up, intercepting 0x01 messages before y-websocket sees them. The hook reinstalls on each reconnect via `provider.on("status", "connected")` + `setTimeout`. **This depends on y-websocket using `ws.onmessage =` (not `addEventListener`).** If a y-websocket upgrade changes this, comment events will silently break. tfm-salima (replace y-websocket with standalone sync) eliminates this coupling.
- **Channel notification meta values MUST be strings.** CC validates `params.meta` against `z.record(z.string(), z.string())`. Non-string values (e.g. `"drift": 466` as int) silently fail Zod validation and the notification is dropped with no error. Always cast: `"drift": str(drift)`. This was the root cause of "notifications silently dropped" — not a CC bug, not timing, just a type mismatch.
- **Cross-machine MCP:** `TAFELMUSIK_URL` is `ws://hezza:3456` (not localhost). `hezza` resolves to loopback on hezza itself and to the tailscale IP from Mac. One URL, both machines. Plugin version must be bumped for marketplace installs to pick up changes.
- **CC session ID:** Available at `~/.claude/sessions/{PID}.json` containing `sessionId` (UUID), `cwd`, `startedAt`. Useful for routing comments to the correct Claude session from tmux hooks.
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
- **Starlette `Route()` without `methods` is GET-only.** It does NOT accept all HTTP methods — POST will return 405. Always pass `methods=["GET", "POST"]` explicitly.
- **CommentStore in ASGI server is deliberately synchronous.** The persistent SQLite connection handles microsecond ops on a tiny table. Do NOT wrap in `asyncio.to_thread()` — that introduces thread-safety issues on the shared connection. Different from `_query_persisted_rooms` which opens/closes a fresh connection per call.
- **Parent watchdog assumes `claude → uv → python` process tree.** `_parent_watchdog()` checks if the grandparent PID (claude) is alive every 30s. If CC changes how it spawns MCP servers (direct spawn, different wrapper, extra nesting), the watchdog may check the wrong PID. Uses `/proc/{pid}/status` parsing — Linux-only. The session-start hook backstop (`ensure-tafelmusik.sh`) covers failures by killing MCP servers whose grandparent is PID 1.
- **CC session file is eventually consistent.** `~/.claude/sessions/{PID}.json` is rewritten during CC startup — a temporary session ID appears first, then the final (resumed) ID overwrites it. `_get_cc_session_id()` in the MCP server polls the file every 2s until the ID stabilizes (max 10s). Reading once at startup picks up the temporary ID, which doesn't match what `comment.py` discovers later. The `_deferred_session_ws` task handles this: it waits for MCP session capture (`session_ready`), then polls for stability.
- **httpx can't do concurrent WebSocket + HTTP on a shared client.** In tests, use a separate `httpx.AsyncClient()` for HTTP requests when another client holds a WebSocket connection. This is a test-only constraint — production POSTs come from separate processes (tmux, browser).
