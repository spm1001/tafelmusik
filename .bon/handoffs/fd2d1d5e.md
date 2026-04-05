# Handoff — 2026-03-29

session_id: fd2d1d5e-c1c3-45cd-9a97-65c79850018b
purpose: Built Unit 1 — ASGI server + CodeMirror editor with Yjs sync and SQLite persistence

## Done
- Implemented asgi_server.py: TafelmusikWebsocketServer with SQLiteYStore persistence, StarletteWebsocket adapter, create_app() factory (tfm-vocego complete)
- Created cm-entry.js: CodeMirror 6 + y-codemirror.next + y-websocket binding, split/preview/edit tabs, room via ?room= param
- Bundled editor.js (732KB) with esbuild, committed artifact
- Refactored: factory pattern replaces module-level singletons, Starlette WebSocketRoute replaces raw ASGI dispatcher, two-store persistence pattern replaces ready=False workaround
- Enabled squash_after_inactivity_of=60s for update log compaction
- Wrote 4 passing tests: two-client sync, multi-room isolation, HTTP serving, persistence across restart
- Enriched understanding.md with Yjs persistence mental model and Yjs internals reference
- Added 4 new gotchas to CLAUDE.md (import paths, store restore, db_path scoping)

## Gotchas
- `from pycrdt.websocket import ...` NOT `from pycrdt_websocket import ...` — namespace merged in 0.16.0
- `from pycrdt.store import SQLiteYStore` — separate package path from websocket
- YRoom does NOT auto-restore from ystore. Use two-store pattern: transient store reads (`async with store: store.apply_updates(doc)`), fresh store passed to YRoom for writes
- `SQLiteYStore.db_path` is a class variable — use `type()` to create dynamic subclass when db_path comes from a function parameter
- Starlette lifespan + WebsocketServer: `async with ws_server:` inside `@asynccontextmanager` lifespan works cleanly — no conflict (resolves Gotcha 15 from plan)
- y-websocket WebsocketProvider connects to `ws://host:port/roomname` — the path IS the room name

## Risks
- Browser reconnect after server restart is untested visually (y-websocket handles it, but UX not verified)
- `WebSocketRoute("/{room:path}")` has no room name validation — any WebSocket upgrade creates a room
- Tests use hardcoded ports (13470, 13471) — parallel test runs will collide
- 732KB JS bundle will grow with awareness, comments UI, etc.

## Next
- Draw down tfm-fujosu (Unit 2: MCP server — Claude as Yjs peer)
- The spike at `references/prototypes/spike-connection-working.py` validates the pycrdt client connection pattern
- `document.py` earns its existence in Unit 2 — `replace_section` and section parsing
- Server is NOT running — start with `uv run uvicorn tafelmusik.asgi_server:app --host 0.0.0.0 --port 3456`

## Commands
```bash
bon show tfm-fujosu              # Unit 2: MCP server
uv run pytest src/ -v            # 4 tests, all pass
uv run uvicorn tafelmusik.asgi_server:app --host 0.0.0.0 --port 3456  # Start server
```

## Reflection
**Claude observed:** This session required more user intervention than typical because pycrdt-websocket is niche — sparse docs, API changed at 0.16.0, persistence restore pattern undocumented for raw ASGI. Three attempts at persistence (raw SQLite bypass → ready=False with internal store → two-store pattern) before finding the make_ydoc pattern in the Django Channels consumer. The lesson: when a framework seems to lack something obvious, check all its consumer implementations, not just the one you're using. The refactoring pass (factory pattern, Starlette-native routing) was prompted by the user asking "what could be better?" — the reflection produced genuinely better architecture.
**User noted:** "This is really clever — flips the normal model on its head" re: Yjs update-log persistence. Shared Yjs docs, INTERNALS.md, and a Medium article to supplement Claude's knowledge. Observed that their own increased activity was because we were in less-mapped training data territory — they were compensating for lower confidence on Claude's side, and every intervention caught something real.
