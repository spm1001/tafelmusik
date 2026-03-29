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

The MCP server's connection lifecycle has a specific pattern: httpx client at the top (created in lifespan, shared across rooms), per-room WebSocket connections managed by AsyncExitStack (created lazily on first tool call), and pycrdt Provider tasks syncing each room's Doc. The Y.Text key is "content" everywhere — browser, MCP server, tests — consistency here is load-bearing for sync.

The base Provider has a subtle liveness bug: when the server disconnects, `_run()` exits (channel iterator stops) but `_send_updates()` stays alive forever waiting for local Doc events. The task group never finishes, so the provider task never completes, and dead connections go undetected. SyncAwareProvider fixes both this (cancels the task group when `_run()` exits) and the sync detection problem (intercepts SYNC_STEP2, the message that delivers the server's full state, and exposes a deterministic `synced` event — no sleep heuristic needed).

## Working with niche libraries

pycrdt-websocket is a framework where the persistence docs lag the capabilities. The API surface is small but the extension points aren't where you'd expect. When you can't find a documented way to do something (like restoring state on startup), check ALL the framework's consumer implementations — the Django Channels consumer has `make_ydoc()` which embodies the pattern the raw ASGI server lacks documentation for. The lesson generalises: niche libraries often have the pattern you need, just in a different consumer/adapter than the one you're using. Reading one adapter's source is cheaper than inventing your own approach.

## Async testing landmines

The anyio/asyncio boundary is real: aconnect_ws (httpx-ws) uses anyio cancel scopes that are bound to the asyncio Task that entered them. This means test fixtures (which may teardown in a different task) can't manage WebSocket lifetimes — use @asynccontextmanager helpers instead. This is a Python 3.14 + anyio interaction, not a pycrdt-specific issue.

## Document operations

replace_section finds headings by exact text match, not by position. This matters because in a CRDT, positions shift as peers edit concurrently. Text matching is stable; index-based matching isn't. The section boundary algorithm (same-or-higher heading level) matches CommonMark's implicit section structure.
