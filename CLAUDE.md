# Tafelmusik

Collaborative editing layer — Yjs Y.Text + CodeMirror 6 + pycrdt. Both Sameer and Claude edit the same CRDT document as Yjs peers.

## Architecture

Three processes, one codebase:

1. **ASGI server** (`asgi_server.py`) — always-running (systemd). Holds Y.Doc, serves web editor, handles WebSocket sync.
2. **MCP server** (`mcp_server.py`) — ephemeral (spawned by CC). Connects to ASGI server via WebSocket as pycrdt client. Provides tools: `read_doc`, `edit_doc`, `load_doc`, `add_comment`, `list_comments`, `export_to_docs`.
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
- **Persistence:** SQLiteYStore. Subclass with `db_path = "data/tafelmusik.db"`.
- **Version:** Single source in `.claude-plugin/plugin.json`.
- **Tests:** Adjacent to source (`*_test.py`). pytest + pytest-asyncio.
- **Comments:** Point + quote architecture. Single StickyIndex anchor + stored quote text. Re-anchor by text search after replace_section.

## Gotchas

- `WebsocketServer.start()` blocks forever — run as `asyncio.create_task()`, never `await` directly.
- `get_room()` is a coroutine — must be awaited.
- `HttpxWebsocket(ws, room_name)` wraps an existing httpx-ws connection, not a URL.
- `StickyIndex.new(text, idx, Assoc.AFTER)` — constructor `StickyIndex(text, idx)` doesn't work.
- `observe()` callbacks are synchronous — use `asyncio.Queue.put_nowait()` + async consumer.
- Python MCP SDK: custom notifications require low-level `Server` API, not FastMCP.
