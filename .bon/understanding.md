# Tafelmusik — Project Understanding

Tafelmusik is a collaborative editing layer where Sameer (human, browser) and Claude (AI, MCP) co-author markdown documents via a shared Yjs CRDT. The core architectural insight: Y.Text (a plain markdown string) was chosen over Y.XmlFragment (rich document tree) because Claude can safely read and write plain text via pycrdt, while the XML tree structure corrupts under non-browser mutations.

Three processes, one codebase: ASGI server (always-running, holds Y.Doc), MCP server (ephemeral, Claude's hands), channel server (ephemeral, push notifications). They're separate because the ASGI server must outlive Claude Code sessions — Sameer needs the editor even when Claude isn't active.

Comments use point+quote anchoring: a single StickyIndex marks where the comment is, a stored quote string says what text it refers to. This is more resilient than start/end ranges because `replace_section` (Claude's primary editing mode) destroys StickyIndex anchors by deleting their containing text. With point+quote, re-anchoring is a text search; with ranges, both endpoints collapse.

The pycrdt-websocket 0.16.0 API has several gotchas validated by spike: `WebsocketServer.start()` blocks forever (must be a task), `HttpxWebsocket` wraps existing connections (not URLs), `get_room()` is async. These are in the plan's Gotchas section and CLAUDE.md.

## How Yjs persistence actually works

Yjs never stores documents. It stores a log of binary updates (deltas) — each mutation produces a small update that is appended to the store. To reconstruct a document, you replay the updates into an empty `Doc`. This is the fundamental mental model; everything follows from it.

SQLiteYStore handles three concerns: (1) appending new updates, (2) squashing old updates into a single compacted update after a period of inactivity (`squash_after_inactivity_of`), and (3) writing periodic checkpoints for fast restore (skip replaying ancient history). The store is a write-side concern — it does NOT auto-restore state when a room starts.

Restoring state on server restart is the developer's responsibility. The pattern comes from pycrdt-websocket's Django Channels consumer, which has a `make_ydoc()` hook: create a Doc, populate it from the store, pass it to the room. For the raw ASGI server, the equivalent is overriding `WebsocketServer.get_room()` to pre-populate a Doc before creating the YRoom. The key implementation detail: use a transient store instance to read persisted updates (via `async with store:` + `store.apply_updates(doc)`), then pass the populated Doc and a *fresh* store instance to `YRoom(ydoc=doc, ystore=fresh_store)`. Two store instances, same db_path — one reads and closes, one writes ongoing. This avoids lifecycle conflicts (the store's `start()` blocks forever, and YRoom manages its own store lifecycle internally).

If you find yourself wanting to "just read the SQLite directly" or "bypass the store API," stop — you're probably fighting the lifecycle rather than understanding it. The two-instance pattern is the clean solution.

For deep understanding of how the CRDT actually works — update encoding, state vectors, the sync protocol, why replaying updates in any order converges — read https://github.com/yjs/yjs/blob/main/INTERNALS.md. This matters most for Unit 3 (channel server), where we need to interpret Y.Text updates to produce semantic change summaries.

## MCP server connection lifecycle

The MCP server's connection lifecycle has a specific pattern: httpx client at the top (created in lifespan, shared across rooms), per-room WebSocket connections created lazily on first tool call, and a standalone sync loop syncing each room's Doc. The Y.Text key is "content" everywhere — browser, MCP server, tests — consistency here is load-bearing for sync.

Each room connection lives inside a `_sync_task()` closure that wraps both `aconnect_ws` (the WebSocket) and `_sync_loop()` in a single asyncio Task. This isn't arbitrary indirection — it's required by anyio's cancel scope model. `aconnect_ws` pushes cancel scopes that are bound to the asyncio Task that entered them. If you open the WebSocket in one task and run the sync loop in another, WebSocket interactions trigger cross-task cancel scope violations: `RuntimeError: Attempted to exit cancel scope in a different task`. The `_sync_task()` pattern keeps everything in one task.

The sync protocol is implemented in ~40 lines using only public pycrdt APIs: `create_sync_message` (initiates handshake), `handle_sync_message` (processes incoming sync messages), `create_update_message` (broadcasts local changes), and `doc.events()` (async iterator for local mutations). An anyio `create_task_group` runs the receive loop, `_send_updates`, and optionally `_heartbeat` concurrently, with the task group cancelled when the channel iterator ends (connection lost). Sync detection is built in: the `synced` event fires when SYNC_STEP2 is received (the server's full state), and the `dead` event fires when the sync task exits for any reason.

The channel abstraction (`Channel` Protocol + `WebSocketChannel` adapter) decouples the sync protocol from httpx-ws specifics. `WebSocketChannel` is a ~15-line wrapper around `send_bytes`/`receive_bytes` with a send lock. `MockChannel` in `conftest.py` implements the same Protocol for unit testing — it auto-responds to SYNC_STEP1 with SYNC_STEP2 when given a peer Doc, enabling deterministic sync testing without a real server.

A keepalive mechanism detects silently dead connections: after `keepalive` seconds of silence, `_heartbeat` sends a SYNC_STEP1 probe (protocol-correct — the server responds with SYNC_STEP2). If no response arrives within `min(keepalive, 10)` more seconds, the task group is cancelled and the connection is marked dead. Disabled (keepalive=None) in tests to avoid timing sensitivity.

## Working with niche libraries

pycrdt-websocket is a framework where the persistence docs lag the capabilities. The API surface is small but the extension points aren't where you'd expect. When you can't find a documented way to do something (like restoring state on startup), check ALL the framework's consumer implementations — the Django Channels consumer has `make_ydoc()` which embodies the pattern the raw ASGI server lacks documentation for. The lesson generalises: niche libraries often have the pattern you need, just in a different consumer/adapter than the one you're using. Reading one adapter's source is cheaper than inventing your own approach.

## Async testing landmines

The anyio/asyncio boundary is real: aconnect_ws (httpx-ws) uses anyio cancel scopes that are bound to the asyncio Task that entered them. This means test fixtures (which may teardown in a different task) can't manage WebSocket lifetimes — use @asynccontextmanager helpers instead. This is a Python 3.14 + anyio interaction, not a pycrdt-specific issue.

The `connect_peer()` test helper in `conftest.py` handles this correctly: it wraps `aconnect_ws` + `_sync_loop` in a single `asyncio.create_task`, waits for deterministic `synced` event (SYNC_STEP2 detection) instead of sleeping, and yields a `Text` object for the test to use. All test files should use `connect_peer()` instead of raw `Provider` + sleep for simulating browser clients.

## Debugging across library boundaries

When N libraries interact and something breaks, bisect the layers before theorising. The approach that cracked the cancel scope bug: bare pycrdt write (works) → write with observer (works) → write with background `asyncio.create_task` (works) → write with Provider (crashes). Each step adds one layer; the failing step isolates the interaction. This took 5 minutes after 20 minutes of theorising about anyio task groups and event types found nothing. The lesson generalises: test each pair of libraries in isolation before reasoning about the whole stack.

## Document operations

replace_section finds headings by exact text match, not by position. This matters because in a CRDT, positions shift as peers edit concurrently. Text matching is stable; index-based matching isn't. The section boundary algorithm (same-or-higher heading level) matches CommonMark's implicit section structure.
