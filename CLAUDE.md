# Tafelmusik

Collaborative editing layer — Yjs Y.Text + CodeMirror 6 + pycrdt. Both Sameer and Claude edit the same CRDT document as Yjs peers.

## Architecture

Three processes, one codebase:

1. **ASGI server** (`asgi_server.py`) — always-running (systemd). Holds Y.Doc, serves web editor, handles WebSocket sync.
2. **MCP server** (`mcp_server.py`) — ephemeral (spawned by CC). Connects to ASGI server via WebSocket as pycrdt client. Provides tools: `read_doc`, `edit_doc`, `load_doc`, `list_docs` (+ future: `add_comment`, `list_comments`, `export_to_docs`).
3. **Channel server** (`channel_server.py`) — ephemeral (spawned by CC). Observes Y.Text changes, pushes notifications.

## Module Layout

```
src/tafelmusik/
  asgi_server.py       # ASGI entry point
  mcp_server.py        # MCP entry point
  channel_server.py    # Channel entry point
  document.py          # Y.Text operations (shared by MCP + channel)
  comments.py          # Y.Map comment operations + StickyIndex
  uploads.py           # Image upload handling
  *_test.py            # Tests adjacent to source
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

## Key Conventions

- **Port:** 3456 (ASGI server). MCP/channel servers discover via `TAFELMUSIK_URL` env var.
- **Persistence:** SQLiteYStore stores updates (not documents). Squashes after 60s idle. See `.bon/understanding.md` for the full mental model.
- **Version:** Single source in `.claude-plugin/plugin.json`.
- **Tests:** Adjacent to source (`*_test.py`). pytest + pytest-asyncio.
- **Comments:** Point + quote architecture. Single StickyIndex anchor + stored quote text. Re-anchor by text search after replace_section.

## Gotchas

- `WebsocketServer.start()` blocks forever — run as `asyncio.create_task()`, never `await` directly.
- `get_room()` is a coroutine — must be awaited.
- `HttpxWebsocket(ws, room_name)` wraps an existing httpx-ws connection, not a URL.
- Import path is `from pycrdt.websocket import ...` NOT `from pycrdt_websocket import ...` (merged namespace in 0.16.0).
- `SQLiteYStore` is in `from pycrdt.store import SQLiteYStore`, NOT `pycrdt.websocket`.
- YRoom does NOT auto-restore from ystore on startup. Use the two-instance pattern: transient store reads via `apply_updates()`, fresh store passed to YRoom for writes. See `_restore_ydoc()` in `asgi_server.py`.
- `SQLiteYStore.db_path` is a class variable. To set it dynamically, use `type("Store", (SQLiteYStore,), {"db_path": path})` — class bodies can't see enclosing function locals.
- `StickyIndex.new(text, idx, Assoc.AFTER)` — constructor `StickyIndex(text, idx)` doesn't work.
- `observe()` callbacks are synchronous — use `asyncio.Queue.put_nowait()` + async consumer.
- Python MCP SDK: custom notifications require low-level `Server` API, not FastMCP.
- `Provider.started` event fires BEFORE sync completes — it only means the task group started. Use `SyncAwareProvider` (in `mcp_server.py`) which intercepts SYNC_STEP2 and exposes a deterministic `synced` event.
- Base `Provider._run()` exits on disconnect but `_send_updates()` stays alive, so the provider task never completes. `SyncAwareProvider` cancels the task group scope when `_run()` exits, enabling dead connection detection via the `dead` Event.
- `aconnect_ws` uses anyio cancel scopes that are task-bound. The WebSocket and Provider must run in the SAME asyncio Task — `_provider_task()` in `mcp_server.py` wraps both. Separating them across tasks (e.g., opening the WebSocket in a tool handler and running the Provider via `asyncio.create_task()`) causes `RuntimeError: Attempted to exit cancel scope in a different task`. Same applies to test fixtures — use `@asynccontextmanager` helpers so `__aenter__`/`__aexit__` run in the same Task.
- `text[start:end] = new_content` does delete+insert in a single Y.Text transaction. Prefer over separate `del` + `insert` calls.
- `text.clear()` removes all content (equivalent to `del text[:]`).
