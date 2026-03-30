# Tafelmusik — Project Understanding

Tafelmusik is a collaborative editing layer where Sameer (human, browser) and Claude (AI, MCP) co-author markdown documents via a shared Yjs CRDT. The core architectural insight: Y.Text (a plain markdown string) was chosen over Y.XmlFragment (rich document tree) because Claude can safely read and write plain text via pycrdt, while the XML tree structure corrupts under non-browser mutations.

Two processes, one codebase: ASGI server (always-running, holds Y.Doc) and MCP server (ephemeral, Claude's brain). The original design had a third process — a channel server for push notifications — but transaction origins in pycrdt are local to a Doc instance (they don't survive binary encoding over the wire), so the change observer that filters Claude's own edits MUST live inside the MCP server process (same Doc). The architecture collapsed from three to two because of this empirically validated constraint. The ASGI server must outlive Claude Code sessions — Sameer needs the editor even when Claude isn't active. The MCP server is becoming more than just tools: it holds the observer, authorship awareness, and notification logic. Formatting attributes (attrs on `text.insert`) DO survive the wire and persistence, making per-character authorship tracking viable — origins for filtering, attrs for capture.

Neither the ASGI server nor the MCP server depends on `pycrdt-websocket`. The entire sync protocol — both server-side and client-side — uses only public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`. The only pycrdt dependencies are `pycrdt` (core) and `pycrdt-store` (SQLiteYStore). This was a deliberate choice to eliminate version-pinning anxiety from private API coupling in a 24/7 server process.

Comments use point+quote anchoring: a single StickyIndex marks where the comment is, a stored quote string says what text it refers to. This is more resilient than start/end ranges because `replace_section` (Claude's primary editing mode) destroys StickyIndex anchors by deleting their containing text. With point+quote, re-anchoring is a text search; with ranges, both endpoints collapse.

## How Yjs persistence actually works

Yjs never stores documents. It stores a log of binary updates (deltas) — each mutation produces a small update that is appended to the store. To reconstruct a document, you replay the updates into an empty `Doc`. This is the fundamental mental model; everything follows from it.

SQLiteYStore handles three concerns: (1) appending new updates, (2) squashing old updates into a single compacted update after a period of inactivity (`squash_after_inactivity_of`), and (3) writing periodic checkpoints for fast restore (skip replaying ancient history). The store is a write-side concern — it does NOT auto-restore state when a room starts.

Restoring state on startup is the developer's responsibility. The pattern: use a transient store instance to read persisted updates (via `async with store:` + `store.apply_updates(doc)`), then create a *fresh* store instance for ongoing writes. Two store instances, same db_path — one reads and closes, one writes ongoing. This avoids lifecycle conflicts. See `_restore_ydoc()` in `asgi_server.py`.

If you find yourself wanting to "just read the SQLite directly" or "bypass the store API," stop — you're probably fighting the lifecycle rather than understanding it. The two-instance pattern is the clean solution.

For deep understanding of how the CRDT actually works — update encoding, state vectors, the sync protocol, why replaying updates in any order converges — read https://github.com/yjs/yjs/blob/main/INTERNALS.md. This matters most for the channel server, where we need to interpret Y.Text updates to produce semantic change summaries.

## ASGI server room management

The ASGI server owns its room management directly via `Room` and `RoomManager` classes (~100 lines). No WebsocketServer, no YRoom — just public pycrdt APIs.

Each `Room` runs a single background task (`_run`) that manages both broadcasting and persistence. The task nests two async contexts: `async with self._store:` (opens the SQLiteYStore connection) and `async with self.doc.events()` (subscribes to Doc changes). The ready event is set only after both contexts are entered AND the store's DB is initialized (`await _store.db_initialized.wait()`), so `serve()` is safe to call immediately after `start()` returns.

Broadcasting uses `doc.events()` — when any client's `handle_sync_message` modifies the Doc, the event fires and the broadcast loop sends `create_update_message(event.update)` to all connected channels concurrently (`asyncio.gather`). This includes the channel that sent the original update; Yjs deduplicates on the client side. Persistence happens in the same loop: `store.write(event.update)` appends to SQLite after each broadcast.

Client connections are handled by `serve()`: add channel to set, send SYNC_STEP1, enter receive loop, remove channel on disconnect. When the last client disconnects, `RoomManager.remove_if_empty()` stops the room (cancels the background task, closes the store) and removes it from the dict. Next connection to the same room restores from SQLite via `_restore_ydoc()`. This means rooms exist in memory only while clients are connected.

The `StarletteWebsocket` adapter wraps Starlette's WebSocket as an async message channel. It swallows `WebSocketDisconnect` and `RuntimeError` on sends — the receive loop detects dead connections via `StopAsyncIteration`. This is important because the broadcast loop sends to all channels; a dead channel must not crash the room.

Lifecycle logging (room created/restored, client connected/disconnected, room cleaned up) goes through `logging.getLogger(__name__)`. Uvicorn doesn't configure the root logger, so `logging.basicConfig(level=logging.INFO)` in the module ensures these reach stderr/journalctl.

## MCP server connection lifecycle

The MCP server's connection lifecycle has a specific pattern: httpx client at the top (created in lifespan, shared across rooms), per-room WebSocket connections created lazily on first tool call, and a standalone sync loop syncing each room's Doc. The Y.Text key is "content" everywhere — browser, MCP server, tests — consistency here is load-bearing for sync.

Each room connection lives inside a `_sync_task()` closure that wraps both `aconnect_ws` (the WebSocket) and `_sync_loop()` in a single asyncio Task. This isn't arbitrary indirection — it's required by anyio's cancel scope model. `aconnect_ws` pushes cancel scopes that are bound to the asyncio Task that entered them. If you open the WebSocket in one task and run the sync loop in another, WebSocket interactions trigger cross-task cancel scope violations: `RuntimeError: Attempted to exit cancel scope in a different task`. The `_sync_task()` pattern keeps everything in one task.

The sync protocol is implemented in ~40 lines using only public pycrdt APIs: `create_sync_message` (initiates handshake), `handle_sync_message` (processes incoming sync messages), `create_update_message` (broadcasts local changes), and `doc.events()` (async iterator for local mutations). An anyio `create_task_group` runs the receive loop, `_send_updates`, and optionally `_heartbeat` concurrently, with the task group cancelled when the channel iterator ends (connection lost). Sync detection is built in: the `synced` event fires when SYNC_STEP2 is received (the server's full state), and the `dead` event fires when the sync task exits for any reason.

The "same asyncio Task" constraint for `aconnect_ws` is specifically about cancel scope lifecycle — entering and exiting the WebSocket context manager must happen in the same Task. It does NOT prevent subtask I/O: anyio's `_spawn` calls `create_task`, so subtasks are separate asyncio Tasks, but they can safely call `send_bytes`/`receive_bytes` on a WebSocket opened by the parent because those operations don't enter or exit cancel scopes. Structured concurrency guarantees subtasks are cancelled before the parent's scope exits, so cleanup order is well-defined. This distinction matters for extending the sync protocol with additional concurrent tasks.

The channel abstraction (`Channel` Protocol + `WebSocketChannel` adapter) decouples the sync protocol from httpx-ws specifics. `WebSocketChannel` is a ~15-line wrapper around `send_bytes`/`receive_bytes` with a send lock. `MockChannel` in `conftest.py` implements the same Protocol for unit testing — it auto-responds to SYNC_STEP1 with SYNC_STEP2 when given a peer Doc, enabling deterministic sync testing without a real server.

A keepalive mechanism detects silently dead connections: after `keepalive` seconds of silence, `_heartbeat` sends a SYNC_STEP1 probe (protocol-correct — the server responds with SYNC_STEP2). If no response arrives within `min(keepalive, 10)` more seconds, the task group is cancelled and the connection is marked dead. Disabled (keepalive=None) in tests to avoid timing sensitivity.

## Deployment

Push-to-deploy on hezza via three git hooks, all calling shared `deploy/restart-if-changed.sh`. The hooks filter for non-test `src/tafelmusik/*.py` changes, restart `tafelmusik.service`, and health-check the API. `post-commit` handles local commits, `post-merge` handles pulls, `post-receive` handles pushes from Mac. The repo accepts pushes via `receive.denyCurrentBranch = updateInstead` (git config, not version-controlled — must be re-set after re-cloning).

## Working with niche libraries

pycrdt-websocket is a framework where the persistence docs lag the capabilities. The API surface is small but the extension points aren't where you'd expect. When you can't find a documented way to do something (like restoring state on startup), check ALL the framework's consumer implementations — the Django Channels consumer has `make_ydoc()` which embodies the pattern the raw ASGI server lacks documentation for. The lesson generalises: niche libraries often have the pattern you need, just in a different consumer/adapter than the one you're using. Reading one adapter's source is cheaper than inventing your own approach.

However: understanding the library internals to build your own clean implementation is even better than subclassing. Both the ASGI server and MCP server replaced pycrdt-websocket abstractions with standalone code using public APIs. The resulting code is shorter, has no version-pinning risk, and is fully understandable without reading library source. The lesson: use library source to understand the *protocol*, then implement the protocol yourself using public APIs.

## Async testing landmines

The anyio/asyncio boundary is real: aconnect_ws (httpx-ws) uses anyio cancel scopes that are bound to the asyncio Task that entered them. This means test fixtures (which may teardown in a different task) can't manage WebSocket lifetimes — use @asynccontextmanager helpers instead. This is a Python 3.14 + anyio interaction, not a pycrdt-specific issue.

The `connect_peer()` test helper in `conftest.py` handles this correctly: it wraps `aconnect_ws` + `_sync_loop` in a single `asyncio.create_task`, waits for deterministic `synced` event (SYNC_STEP2 detection) instead of sleeping, and yields a `Text` object for the test to use. All test files should use `connect_peer()` instead of raw connections for simulating browser clients.

## Debugging across library boundaries

When N libraries interact and something breaks, bisect the layers before theorising. The approach that cracked the cancel scope bug: bare pycrdt write (works) → write with observer (works) → write with background `asyncio.create_task` (works) → write with Provider (crashes). Each step adds one layer; the failing step isolates the interaction. This took 5 minutes after 20 minutes of theorising about anyio task groups and event types found nothing. The lesson generalises: test each pair of libraries in isolation before reasoning about the whole stack.

## Persistence landmine: pycrdt-store squashing

pycrdt-store 0.1.3 has a data-loss bug in SQLiteYStore.write() — the inline squash path has `ydoc.apply_update(update)` indented inside `if self._decompress:`, so without compression (the default) it replays into an empty Doc and destroys all stored updates. Tafelmusik disables squashing (`squash_after_inactivity_of=None`) as a workaround — correctness preserved, but the yupdates table grows unboundedly. Upstream fix is PR y-crdt/pycrdt-store#25. Re-enable squashing after the fix ships. If the upstream repo stays dormant, the correctly-written `_squash_document_history()` method in the PR could serve as a reference for owning persistence ourselves.

## Document operations

replace_section finds headings by exact text match, not by position. This matters because in a CRDT, positions shift as peers edit concurrently. Text matching is stable; index-based matching isn't. The section boundary algorithm (same-or-higher heading level) matches CommonMark's implicit section structure.
