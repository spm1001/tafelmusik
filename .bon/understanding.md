# Tafelmusik — Project Understanding

Tafelmusik is a collaborative editing layer where Sameer (human, browser) and Claude (AI, MCP) co-author markdown documents via a shared Yjs CRDT. The core architectural insight: Y.Text (a plain markdown string) was chosen over Y.XmlFragment (rich document tree) because Claude can safely read and write plain text via pycrdt, while the XML tree structure corrupts under non-browser mutations.

Two processes, one codebase: ASGI server (always-running, holds Y.Doc) and MCP server (ephemeral, Claude's brain). The original design had a third process — a channel server for push notifications — but transaction origins in pycrdt are local to a Doc instance (they don't survive binary encoding over the wire), so the change observer that filters Claude's own edits MUST live inside the MCP server process (same Doc). The architecture collapsed from three to two because of this empirically validated constraint. The ASGI server must outlive Claude Code sessions — Sameer needs the editor even when Claude isn't active. The MCP server is becoming more than just tools: it holds the observer, authorship awareness, and notification logic. Formatting attributes (attrs on `text.insert`) DO survive the wire and persistence, making per-character authorship tracking viable — origins for filtering, attrs for capture.

Neither the ASGI server nor the MCP server depends on `pycrdt-websocket`. The entire sync protocol — both server-side and client-side — uses only public pycrdt APIs: `create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`. The only pycrdt dependencies are `pycrdt` (core) and `pycrdt-store` (SQLiteYStore). This was a deliberate choice to eliminate version-pinning anxiety from private API coupling in a 24/7 server process.

## The files-on-disk pivot (calute)

The current architecture stores documents only as Yjs updates in SQLite — no .md files on disk. This creates a coupling that prevents editing documents with other tools, git-tracking content, or pointing Tafelmusik at an existing folder of markdown. The resolved design: **the .md file is truth, the CRDT is the collaboration overlay.**

Three phases, all designed and agreed:

**Phase 1 — File-backed rooms:** `docs_dir` parameter on ASGI server (default `ROOT / "docs"`). Room names = relative file paths (`meeting/2026-03-30` → `docs/meeting/2026-03-30.md`). Hydrate CRDT from file on room open, fall back to SQLite for migration. New `flush_doc` MCP tool writes Y.Text to .md, wipes comments, discards CRDT log. Directory listing replaces room-based `list_docs`. New doc flow: navigate to room name → type → flush creates the .md file.

**Hydrate has three paths:** File exists → hydrate from file (fresh Doc, `text += content`). No file but SQLite data → fall back to `_restore_ydoc`. Neither → empty Doc. The current `_restore_ydoc` creates its own Doc internally; the new `get_room()` must create the Doc itself for the file path.

**Flush is explicit** (Claude-initiated). If a session crashes before flush, edits exist only in CRDT/SQLite — acceptable since CRDT handles crash recovery during session. Comments wipe on flush because they're ephemeral session annotations, not durable content.

**The CRDT duplication trap:** Every file hydration creates NEW CRDT operations (new client ID, new clocks). Any peer holding old operations will merge both sets — producing duplicated content. Yjs merges by operation identity, not content identity: two inserts of "hello" from different client IDs produce "hellohello". Room retention (keeping file-backed rooms in memory) prevents duplication on idle reconnects, but not across server restarts where the room is necessarily fresh. SQLite preserves operation identity across room lifecycles within a process; file hydration should be treated as a one-time seed, not a repeatable operation. The browser-side fix (tfm-wiseha) is: detect server restart and discard the stale local Y.Doc before reconnecting.

**Phase 2 — Done (tfm-becitu, tfm-semame, tfm-nocaga).** Comment operations extracted to `comments.py`. Y.Map observer replaced Y.Text observer for notifications — document-body edits produce silence, comments are the turn signal. Drift tracking via state vector snapshots: `doc.get_update(snapshot)` byte size measures staleness, gates full-doc pushes alongside comments or on idle timeout. Three push triggers: (a) comment + high drift = full doc + comment, (b) idle 30s + high drift = automatic resync, (c) room connect = initial context push. `DRIFT_THRESHOLD` (1024B) and `IDLE_TIMEOUT` (30s) are tuning parameters — journalctl logs drift scores on every comment and idle timer fire for empirical tuning. Remaining: tfm-napari (remove `read_doc` from tool surface).

**Phase 3 — Spawn-on-comment (tfm-rohudu):** When a comment arrives with no MCP peer connected, spawn Claude on hezza to respond.

**Implementation brief** lives in `docs/calute-phase2-brief.md` with sequencing constraints and pseudocode. The spike tests (`calute_spike_test.py`, 9 tests) validate hydrate→edit→comment→flush→re-hydrate round-trip at the pure pycrdt level — production goes through RoomManager, WebSocket sync, two processes.

## Comments: inline reactions, not editorial workflow

Comments use point+quote anchoring: a single StickyIndex marks where the comment is, a stored quote string says what text it refers to. This is more resilient than start/end ranges because `replace_section` (Claude's primary editing mode) destroys StickyIndex anchors by deleting their containing text. With point+quote, re-anchoring is a text search; with ranges, both endpoints collapse.

Cross-implementation compatibility is verified: pycrdt's `StickyIndex.to_json()` and Yjs's `Y.relativePositionToJSON()` produce the same `{item: {client, clock}, assoc}` structure — no adaptation layer needed. The only subtlety: `Assoc.AFTER` in pycrdt maps to `assoc=0` in Yjs (both mean right association), but resolved positions can differ by 1 character at insertion boundaries. The overlap-based sort compensates for this in comment card ordering.

The browser-side comment UI uses a two-state machine (`document`/`commenting`) rather than scattered boolean flags. This pattern emerged after three iterations of ad-hoc conditional logic — when 3+ interacting booleans control UI visibility, there's a state machine hiding in them. Card clicks use CM6 `Annotation` to mark programmatic selections, preventing false state transitions. The cost of extracting the state machine early is low; the cost of waiting compounds with every new UI feature that adds another conditional branch.

The clearest articulation of why comments matter came during /close: Sameer wanted to react to specific parts of Claude's reflection but had to paste-and-interleave or use ambiguous number references. Comments aren't about code review or editorial workflow — they're about enabling inline reactions to specific text, from either party, without breaking the document flow. This reframes tfm-melemu: the UI affordance (a side pane linked to text selections) matters more than the backend (StickyIndex + Y.Map already exist). The dependency chain is: clean up the editor (tfm-kosotu) -> repurpose the split pane as a comments sidebar -> wire to the Y.Map backend (tfm-melemu).

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

When N libraries interact and something breaks, bisect the layers before theorising. The approach that cracked the cancel scope bug: bare pycrdt write (works) -> write with observer (works) -> write with background `asyncio.create_task` (works) -> write with Provider (crashes). Each step adds one layer; the failing step isolates the interaction. This took 5 minutes after 20 minutes of theorising about anyio task groups and event types found nothing. The lesson generalises: test each pair of libraries in isolation before reasoning about the whole stack.

## Persistence landmine: pycrdt-store squashing

pycrdt-store 0.1.3 has a data-loss bug in SQLiteYStore.write() — the inline squash path has `ydoc.apply_update(update)` indented inside `if self._decompress:`, so without compression (the default) it replays into an empty Doc and destroys all stored updates. Tafelmusik disables squashing (`squash_after_inactivity_of=None`) as a workaround — correctness preserved, but the yupdates table grows unboundedly. Upstream fix is PR y-crdt/pycrdt-store#25. Re-enable squashing after the fix ships. If the upstream repo stays dormant, the correctly-written `_squash_document_history()` method in the PR could serve as a reference for owning persistence ourselves. With calute, flush becomes the compaction — sidesteps the squashing bug entirely for file-backed rooms.

## Document operations

replace_section finds headings by exact text match, not by position. This matters because in a CRDT, positions shift as peers edit concurrently. Text matching is stable; index-based matching isn't. The section boundary algorithm (same-or-higher heading level) matches CommonMark's implicit section structure.

## Multi-action choreography

Bons capture what (--what) and why (--why) per action, but not the execution ordering between actions. This gap becomes dangerous when an outcome has actions with sequencing dependencies — calute Phase 2 requires four actions in strict order (becitu → semame → nocaga → napari) because swapping the observer before drift tracking is ready makes Claude completely deaf to major surgery. The --what fields are self-contained per action, but the constraint lives *between* them. The workaround is an implementation brief (`docs/calute-phase2-brief.md`) referenced from the outcome. This is convention, not structure — if a future Claude skips the brief, they'll implement in isolation and hit the dangerous partial state. Filed bon-tufihu in the bon repo for a proper --how mechanism. Any multi-action outcome with ordering dependencies needs this kind of choreography documentation.

## Token efficiency insight

CRDTs are already more token-efficient than a file-based approach would be. The CRDT pushes diffs over the wire — adding a file layer on top would mean hauling full file content in and out of context. The drift threshold (how much CRDT state can diverge from the file before forcing a resync) is a Goldilocks problem: too low = wasted resync tokens, too high = wasted failed-edit tokens. Comments serve as turn signals rather than continuous notifications — comment events notify Claude, not every keystroke.

## Working pattern: review while context is hot

The review-then-fix loop — asking "what did we miss, what could be better, what might go wrong" immediately after implementing, before committing — catches real issues that would otherwise become fix-up bons. Examples from Phase 2: consumer task not awaited on cancellation, no drift logging for threshold tuning, orphaned `_cached_content` field. The cost is 5 minutes of re-reading; the payoff is shipping cleaner code in the same session. Review while context is hot is qualitatively different from review after the fact — you see things in code you just wrote that you'd miss coming back cold.
