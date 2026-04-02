# Tafelmusik — Project Understanding

Tafelmusik is a collaborative editing layer where Sameer (human, browser) and Claude (AI, MCP) co-author markdown documents via a shared Yjs CRDT. The core architectural insight: Y.Text (a plain markdown string) was chosen over Y.XmlFragment (rich document tree) because Claude can safely read and write plain text via pycrdt, while the XML tree structure corrupts under non-browser mutations.

Two processes, one codebase: ASGI server (always-running, holds Y.Doc) and MCP server (ephemeral, Claude's brain). Transaction origins in pycrdt are local to a Doc instance (they don't survive binary encoding over the wire), so the change observer that filters Claude's own edits MUST live inside the MCP server process. Neither process depends on `pycrdt-websocket` — all sync protocol code uses public pycrdt APIs only (`create_sync_message`, `handle_sync_message`, `create_update_message`, `doc.events()`).

## The architectural rethink (2026-04-01)

The accumulated brittleness of the stack — y-websocket monkey-patched, pycrdt-store squashing disabled, signal files as notification workaround, drift tracking for a problem that shouldn't exist — prompted a full reassessment. The conclusion: Tafelmusik is not a document editor with annotations. It's a **messaging layer with one novel property: content-addressed anchoring into shared artifacts.**

### Comments are messages, not annotations

Comments are standalone entities in SQLite, not Y.Map entries in the CRDT. This decouples comment lifecycle from document lifecycle — comments survive flush, restart, CRDT state reset. The W3C Web Annotation Model's `motivation` field maps naturally to the kinds of messages exchanged: instructions, requests, reactions, assessments, suggestions. But no motivation taxonomy is needed — both parties (human and LLM) parse natural language.

Anchoring uses TextQuoteSelector (W3C Web Annotation): quoted text + 30 chars prefix/suffix context. Four-strategy re-anchoring cascade: exact match → disambiguation by context → fuzzy match (SequenceMatcher) → context recovery (find prefix...suffix region when quote is deleted). Comments that can't re-anchor become orphaned but are never deleted — their quoted text carries context even without a position.

Schema is deliberately minimal: id, author, created, target, body, quote, prefix, suffix, replies_to, resolved. Nine fields. The target is an opaque string the system doesn't interpret; surfaces resolve it — same comment system works for documents, conversations, source files, bon items, URLs.

### Why Yjs stays

Deep Research found that CM6's OT collab doesn't guarantee position convergence across peers (Marijn's own caveat), making it worse for annotation anchoring, not better. The "keep CRDT for text, SQLite for comments" split gives convergent concurrent editing where needed (text) and lifecycle independence where needed (comments).

### What's been built

`anchored.py` (200 lines, 28 tests) — the standalone comment system with content-addressed anchoring. `playground.py` — interactive CLI for playing with comments on real files. tmux integration (Ctrl-b C popup for quick reactions anchored to selected text). This eliminated the manual serialisation loop (copy from TUI → paste in editor → structure → paste back).

## The files-on-disk pivot (calute)

The .md file is truth, the CRDT is the collaboration overlay. `docs_dir` parameter on ASGI server (default `~/Repos`). Room names = relative file paths. Hydrate CRDT from file on room open, fall back to SQLite for migration. `flush_doc` writes Y.Text to .md, git commits.

**The CRDT duplication trap:** Every file hydration creates NEW CRDT operations (new client ID, new clocks). Any peer holding old operations will merge both sets — producing duplicated content. Room retention (keeping file-backed rooms in memory) prevents duplication on idle reconnects. The browser-side fix (tfm-wiseha) is: detect server restart and discard the stale local Y.Doc before reconnecting.

## Notification delivery gap

The MCP server's notification pipeline is correct — observer fires, debouncer runs, `send_message()` writes to stdio without error. The break is on the CC side: channel notifications are silently dropped in most REPL states. This is a known CC bug (anthropics/claude-code#36975, #37139, #40237, #36477). Two working push paths into Claude's context exist: (1) mousetrap pattern (background Bash exit → `<task-notification>`), (2) asyncRewake hooks (exit code 2 → `enqueuePendingNotification`). The signal file approach is the current workaround.

## Key technical constraints

- `StickyIndex.new(text, idx, Assoc.AFTER)` — constructor `StickyIndex(text, idx)` doesn't work.
- `observe()` callbacks are synchronous — use `asyncio.Queue.put_nowait()` + async consumer.
- `aconnect_ws` uses anyio cancel scopes that are task-bound — WebSocket and sync loop must run in the SAME asyncio Task.
- pycrdt-store 0.1.3 has a data-loss bug in squashing — disabled, calute's flush sidesteps it.
- y-websocket's hardcoded 30s `messageReconnectTimeout` kills quiet connections — monkey-patched, proper fix is standalone browser sync (tfm-salima).
- `text[start:end] = new_content` strips formatting attrs — use `del` + `insert` to preserve authorship.
- `replace_section` refuses h1 headings (extends to EOF) — use `replace_all` instead.

## Working patterns

- **Bisect async layers:** Test each pair of libraries in isolation before reasoning about the whole stack.
- **Review while context is hot:** Ask "what did we miss" immediately after implementing, before committing.
- **Niche libraries:** Read source to understand the protocol, then implement yourself using public APIs.
