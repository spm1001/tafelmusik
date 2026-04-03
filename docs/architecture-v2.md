# Tafelmusik Architecture v2

## What this system is

Tafelmusik is a **messaging layer with content-addressed anchoring into shared artifacts.** Not a document editor with annotations. Not a collaboration tool with chat. A messaging system where messages can point at specific text in shared documents — and those pointers survive edits.

Two independent layers:

1. **Document layer** — Yjs CRDT text sync (Y.Text + pycrdt). Handles concurrent editing, convergence, persistence. The artifact.
2. **Comment layer** — SQLite messages with TextQuoteSelector anchoring (W3C Web Annotation). Handles conversation, threading, routing. The dialogue.

Neither owns the other. Documents don't know about comments. Comments reference documents but don't depend on the document data structure.

## Why two layers

The original design embedded comments in the CRDT (Y.Map alongside Y.Text). This created coupling:

- Comments died on `flush_doc` (which writes Y.Text to .md and resets the CRDT)
- Comments couldn't be queried across documents
- Comment lifecycle was tied to CRDT lifecycle (server restart = comments gone)
- The Y.Map observer + StickyIndex anchoring was ~300 lines of complex code

The rethink (2026-04-01): comments are messages. The system is a messaging layer. The only novel property is content-addressed anchoring — a message can say "this comment is about *this specific text*" and that pointer survives edits to the document.

## Why Yjs stays for text

Deep Research (2026-04-02) found that CM6's OT collab (`@codemirror/collab`) doesn't guarantee position convergence across peers — Marijn Haverbeke's own caveat. For annotation anchoring, convergent positions matter. Yjs gives convergent concurrent editing where it's needed (text). SQLite gives lifecycle independence where it's needed (comments).

## Anchoring: TextQuoteSelector

Comments anchor to text via W3C Web Annotation's TextQuoteSelector: the quoted text + 30 characters of prefix/suffix context. Four-strategy re-anchoring cascade:

1. **Exact match** — unique hit, high confidence
2. **Disambiguate by context** — multiple exact hits, pick the one whose surrounding text matches prefix/suffix (SequenceMatcher, threshold 0.6)
3. **Fuzzy match** — text was edited slightly, sliding-window SequenceMatcher (threshold 0.7)
4. **Context recovery** — quote is deleted entirely, but prefix/suffix context strings are still present; anchor the gap between them

Comments that can't re-anchor become orphaned but are never deleted — their quoted text carries context even without a position.

### Why not StickyIndex (the old approach)

StickyIndex tracks positions via CRDT item references — they update automatically as the document changes. But `replace_section` (Claude's primary editing mode) deletes the containing text, destroying all StickyIndex anchors within it. With TextQuoteSelector, re-anchoring is a text search — it works regardless of how the edit happened.

## Schema

Deliberately minimal — nine fields:

```
id, author, created, target, body, quote, prefix, suffix, replies_to, resolved
```

- `target` is an opaque string the system doesn't interpret. Surfaces resolve it. Same comment system works for documents, conversations, source files, bon items, URLs.
- No motivation taxonomy — both parties (human and LLM) parse natural language.
- `replies_to` enables threading (depth-first).
- `resolved` is a boolean, not a state machine.

Implementation: `anchored.py` (200 lines, 28 tests). Pure stdlib + SQLite. No CRDT dependency.

## Lazy reanchor

Don't reanchor on every edit. Reanchor when comments are *read* (list endpoint, notification assembly).

Rationale: edits are frequent, reads are infrequent (page load, new comment notification). Stale anchor positions between reads are harmless — the quote text is the source of truth. The browser can reanchor client-side using its local document state.

**Trap:** Don't add reanchor calls to `edit_doc` or the CRDT observer. That's the old StickyIndex pattern. The new system is lazy.

## Channel notifications

The MCP server pushes comment notifications to Claude via `notifications/claude/channel` (MCP experimental capability). Format:

```xml
<channel source="tafelmusik" room="doc/path" type="comment" author="sameer" drift="466">
Comment on 'doc/path' by sameer:
> "quoted text"
Comment body here
</channel>
```

**Critical constraint: all meta values must be strings.** CC validates `params.meta` against `z.record(z.string(), z.string())`. Non-string values (e.g. `drift` as int) silently fail Zod validation and the notification is dropped with no error. This was the root cause of months of "notifications silently dropped" — not a CC bug, just a type mismatch.

### Channel flags

| Machine | Flag |
|---------|------|
| hezza (dev, repo `.mcp.json`) | `--dangerously-load-development-channels server:tafelmusik` |
| Mac (plugin install) | `--dangerously-load-development-channels plugin:tafelmusik@batterie-de-savoir` |

## Session routing

CC exposes session ID at `~/.claude/sessions/{PID}.json`:

```json
{
  "pid": 1474808,
  "sessionId": "bd1318d9-e53f-4a43-8f33-2e2d428dd945",
  "cwd": "/home/modha/Repos/batterie/tafelmusik",
  "startedAt": 1775156059435
}
```

The tmux comment hook grabs the CC process PID from the pane, reads the session file, and includes `session_id` in comment metadata for routing. Each Claude consumes only comments addressed to its session.

## WebSocket protocol

Multiplex on the existing CRDT WebSocket. 1-byte type prefix:

- `0x00` — CRDT sync message (binary, existing)
- `0x01` — Comment event (JSON: new comment, resolved, etc.)

The ASGI server broadcasts comment events to all peers in the room alongside CRDT updates. No second WebSocket.

Browser uses HTTP for comment CRUD (fetch API is simpler than WS request-response from JS). MCP server uses WS (already has a connection per room). Both write to the same SQLite store.

## Migration strategy

Big-bang, not gradual. The Y.Map and SQLite comment systems have incompatible data models (StickyIndex vs TextQuoteSelector). Running both means maintaining two notification pipelines, two observer patterns, two anchor strategies. The old system is small enough (~300 lines) that removing it is cheaper than bridging it.

Sequence within the big-bang:

1. ASGI server: HTTP endpoints + WS broadcast (testable independently)
2. tmux hook: POST to ASGI (validates HTTP design, immediately useful)
3. MCP tools: swap Y.Map calls for WS calls
4. Browser: swap Y.Map reads for HTTP + WS
5. Cleanup: delete `comments.py`, remove observers, simplify `flush_doc`

Each layer tests against the one below it.

### flush_doc simplifies

Currently `flush_doc` calls `comments.clear_all()` to wipe Y.Map comments. With SQLite comments, flush doesn't touch them — they persist by design. Remove the comment-wipe code. A future Claude might add it back thinking "flush should clean up" — no, the whole point of SQLite comments is lifecycle independence.

## Cross-machine setup

`TAFELMUSIK_URL` is `ws://hezza:3456`. `hezza` resolves to loopback on hezza itself (127.0.1.1) and to the tailscale IP from Mac. One URL, both machines.

The ASGI server runs on hezza (systemd). The MCP server runs locally on whatever machine CC is on, connecting to hezza over tailscale. Plugin installs from the marketplace; no git clone needed on Mac.

## What `anchored.py` is built for

The comment system is target-agnostic. `target` is an opaque string — could be a tafelmusik room name, a file path, a CC session ID, a bon item ID, a URL. The anchoring algorithm works on any text. The SQLite store queries by target.

This means the same system handles:
- Document comments (target = room name, anchored to document text)
- Conversation comments (target = session ID, anchored to terminal output)
- Code review comments (target = file path, anchored to source code)

The tmux Ctrl-b C hook already creates comments on terminal output. The interaction model — Sameer comments on Claude's output while Claude keeps working — is the core value proposition. Comments don't require turn-taking.

## Known issues

- **CRDT duplication:** Each server restart re-hydrates from .md file, creating new CRDT ops that merge with SQLite-stored old ops. Content doubles per restart. Mitigated by flushing SQLite periodically; properly fixed by replacing y-websocket with standalone browser sync (tfm-salima).
- **ASGI server hangs under load:** rglob across ~/Repos on every /api/rooms request saturated the event loop. 30s TTL cache (2026-04-02) mitigates; deeper investigation needed for memory/CPU accumulation.
- **File browser is architecturally wrong:** Reimplements `ls` poorly. Finder and filesystem tools already exist. Needs rethinking.
