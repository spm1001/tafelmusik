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
