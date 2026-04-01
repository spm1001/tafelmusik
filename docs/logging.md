# Logging

How to read Tafelmusik's logs when something goes wrong.

## The chain

```
Browser ←WebSocket→ ASGI server ←WebSocket→ MCP server ←stdio→ Claude Code
```

ASGI = Asynchronous Server Gateway Interface. Python's async web server standard — like WSGI but it can hold WebSocket connections open.

There are two log streams, one correlation key: **document path** (called "room" internally — Yjs terminology baked into the code, but it's just the file path).

| Stream | Where | Format |
|--------|-------|--------|
| ASGI server | `journalctl --user -u tafelmusik` | `Room {name}: event` |
| MCP server | Claude Code process stderr | `[tfm] op={tool} room={name} dur={ms}ms status={ok\|error}` |
| Browser | DevTools console | `[tfm] room={name} context` |me} context` |

## Reading a successful edit

```
[tfm] op=edit_doc room=work/narrative mode=patch     ← tool entry
[tfm] op=connect room=work/narrative cached           ← already connected
[tfm] op=edit_doc room=work/narrative dur=45ms status=ok Patched 42 chars  ← done
```

## Reading a timeout

```
[tfm] op=edit_doc room=work/narrative mode=patch     ← tool entry
[tfm] op=connect room=work/narrative                  ← new connection attempt
[tfm] op=connect room=work/narrative dur=10012ms status=timeout  ← sync never completed
[tfm] op=edit_doc room=work/narrative dur=10015ms status=error   ← tool failed
  TimeoutError: Sync with Tafelmusik server timed out...
```

## Reading a patch failure

```
[tfm] op=edit_doc room=work/narrative mode=patch
[tfm] op=connect room=work/narrative cached
[tfm] patch: no match (find_len=187 first_chars='**World Cup.** Event measurement — pro')  ← DEBUG
[tfm] op=edit_doc room=work/narrative dur=12ms status=error ## What to check first

These are Claude commands — run them from a CC session, not the browser.

1. **Is the ASGI server running?** `systemctl --user status tafelmusik`
2. **Is the document active?** `curl -s hezza:3456/api/rooms | jq '.rooms[] | select(.active)'`
3. **Is the MCP server connected?** Look for `op=connect ... status=ok` in recent stderr
4. **Is the content stale?** `inspect_doc` shows drift score — high drift means Claude's model diverged fThe ASGI server is the long-lived process — systemd on hezza, always running. It owns the Y.Doc for each document, persists updates to SQLite, and serves the CodeMirror editor. WebSocket connections get channels; the server broadcasts changes to all channels concurrently. Failed channels are dropped and logged at WARNING (this was silent before today's logging work).

The MCP server is ephemeral — spawned fresh each Claude Code session, dies when the session ends. It connects as a Yjs peer via WebSocket, keeping a local Y.Doc copy. The sync protocol is ~40 lines using public pycrdt APIs only. No pycrdt-websocket dependency — we own the protocol end-to-end.

The browser uses y-websocket's WebsocketProvider for reconnection and sync, and y-codemirror.next to bind Y.Text to CodeMirror 6. Comments live in a Y.Map inside the same Y.Doc.side the Y.Text, both inside the same Y.Doc.

All three peers (browser, ASGI server, MCP server) see the same Y.Doc. Edits from any peer merge automatically via the CRDT — no conflicts, no locking, no last-write-wins. The only coordination is the sync protocol: peers exchange state vectors, compute missing updates, and send them. Order doesn't matter. Timing doesn't matter. Two peers can be offline for hours, reconnect, and merge cleanly.

This is why Tafelmusik uses Y.Text (plain markdown string) instead of Y.XmlFragment (rich document tree). Claude can safely read and write plain text via pycrdt. The XML tree structure corrupts under non-browser mutations because the tree invariants are only enforced by the ProseMirror/Tiptap binding — raw API calls bypass them.tylaude's model diverged from reality
