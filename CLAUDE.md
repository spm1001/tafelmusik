# Tafelmusik

Collaborative editing layer — Yjs Y.Text + CodeMirror 6 + pycrdt. Both Sameer and Claude edit the same CRDT document as Yjs peers.

## Architecture

Three processes, one codebase:

1. **ASGI server** (`asgi_server.py`) — always-running (systemd). Owns room management (Room + RoomManager), serves web editor, handles WebSocket sync. Zero pycrdt.websocket dependency — uses public pycrdt APIs only.
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
- **Port:** 3456 (ASGI server). MCP/channel servers discover via `TAFELMUSIK_URL` env var.
- **Persistence:** SQLiteYStore stores updates (not documents). Squashes after 60s idle. See `.bon/understanding.md` for the full mental model.
- **Version:** Single source in `.claude-plugin/plugin.json`.
- **Tests:** Adjacent to source (`*_test.py`). pytest + pytest-asyncio.
- **Comments:** Point + quote architecture. Single StickyIndex anchor + stored quote text. Re-anchor by text search after replace_section.

## Gotchas

- Neither the ASGI server nor the MCP server imports `pycrdt.websocket`. All sync protocol code uses public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`.
- `SQLiteYStore` is in `from pycrdt.store import SQLiteYStore`, NOT `pycrdt.websocket`.
- SQLiteYStore does NOT auto-restore state. Use the two-instance pattern: transient store reads via `apply_updates()`, fresh store for ongoing writes. See `_restore_ydoc()` in `asgi_server.py`.
- `SQLiteYStore.db_path` is a class variable. To set it dynamically, use `type("Store", (SQLiteYStore,), {"db_path": path})` — class bodies can't see enclosing function locals.
- `StickyIndex.new(text, idx, Assoc.AFTER)` — constructor `StickyIndex(text, idx)` doesn't work.
- `observe()` callbacks are synchronous — use `asyncio.Queue.put_nowait()` + async consumer. The observer callback in `mcp_server.py` has a try/except safety net because an unhandled exception would crash the sync loop.
- **Authorship & origins:** All writes through `document.py` must be wrapped in `doc.transaction(origin=author)` and use `text.insert(..., attrs={"author": author})`. Author constants live in `authors.py`. The MCP observer uses `txn.origin` to filter Claude's own edits — if you add a write path without the origin, Claude gets self-notifications. Origins are local to a Doc instance (not serialized over the wire), which is why the observer must live in the MCP server process, not a separate channel server.
- **`find_section` is code-block-aware.** Headings inside fenced code blocks (``` or ~~~) are ignored. `diff_sections` and `_extract_sections` use the same logic.
- Python MCP SDK: custom notifications require low-level `Server` API, not FastMCP.
- `aconnect_ws` uses anyio cancel scopes that are task-bound. The WebSocket and sync loop must run in the SAME asyncio Task — `_sync_task()` in `mcp_server.py` wraps both. Separating them across tasks causes `RuntimeError: Attempted to exit cancel scope in a different task`. Same applies to test fixtures — use `@asynccontextmanager` helpers so `__aenter__`/`__aexit__` run in the same Task. The `connect_peer()` test helper in `conftest.py` handles this correctly.
- The MCP server owns its sync protocol (~40 lines in `_sync_loop`/`_send_updates`/`_heartbeat`) using only public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`. No Provider subclass, no private API coupling.
- The ASGI server's Room broadcasts via `doc.events()` — one background task per room listens for Doc changes, sends `create_update_message` to all connected channels concurrently (`asyncio.gather`), and persists via `store.write()`. Room.start() waits for both DB initialization and the doc observer before returning, so serve() is safe to call immediately.
- Rooms are cleaned up when the last client disconnects (`remove_if_empty`). Next connection to the same room restores from SQLite.
- No `pycrdt-websocket` dependency anywhere — `pycrdt` + `pycrdt-store` only.
- `text[start:end] = new_content` does delete+insert but strips formatting attrs. Use `del text[start:end]` + `text.insert(start, content, attrs=...)` to preserve authorship. `document.py` handles this — call its functions rather than operating on Text directly.
- `text.clear()` removes all content (equivalent to `del text[:]`).
