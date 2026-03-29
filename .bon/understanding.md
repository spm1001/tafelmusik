# Tafelmusik — Project Understanding

Tafelmusik is a collaborative editing layer where Sameer (human, browser) and Claude (AI, MCP) co-author markdown documents via a shared Yjs CRDT. The core architectural insight: Y.Text (a plain markdown string) was chosen over Y.XmlFragment (rich document tree) because Claude can safely read and write plain text via pycrdt, while the XML tree structure corrupts under non-browser mutations.

Three processes, one codebase: ASGI server (always-running, holds Y.Doc), MCP server (ephemeral, Claude's hands), channel server (ephemeral, push notifications). They're separate because the ASGI server must outlive Claude Code sessions — Sameer needs the editor even when Claude isn't active.

Comments use point+quote anchoring: a single StickyIndex marks where the comment is, a stored quote string says what text it refers to. This is more resilient than start/end ranges because `replace_section` (Claude's primary editing mode) destroys StickyIndex anchors by deleting their containing text. With point+quote, re-anchoring is a text search; with ranges, both endpoints collapse.

The pycrdt-websocket 0.16.0 API has several gotchas validated by spike: `WebsocketServer.start()` blocks forever (must be a task), `HttpxWebsocket` wraps existing connections (not URLs), `get_room()` is async. These are in the plan's Gotchas section and CLAUDE.md.
