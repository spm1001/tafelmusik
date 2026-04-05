# Handoff — 2026-03-30

session_id: be8ecca7-4765-4a70-8072-37f5ec8bf114
purpose: Channel notifications wired end-to-end, pycrdt-store data-loss bug found and reported

## Done
- Completed tfm-decigu: channel notification reaches Claude Code conversation — session capture, JSONRPCNotification via ServerSession.send_message(), experimental capability declared, 3 new tests
- Live-tested bilateral editing loop: browser edits → channel notification → Claude reads changes → responds
- Built inspect_doc MCP tool — shows Y.Text with formatting attributes (the layer str(text) hides)
- Found and confirmed pycrdt-store 0.1.3 data-loss bug: squash destroys data when compression disabled (the default)
- Filed upstream issue y-crdt/pycrdt-store#24 and PR y-crdt/pycrdt-store#25 (two-line fix + regression test, 56/56 tests pass)
- Disabled squashing as workaround (squash_after_inactivity_of=None in asgi_server.py)
- Filed tfm-ditufu: room awareness gap (observer only fires for rooms Claude has connected to)
- Filed tfm-zeguhi: surgical editing grammar (replace_section is a sledgehammer, need patch mode)
- Created docs/editing-grammar.md — living design doc for editing scenarios, 5 initial cases
- Deleted dead channel_server.py stub, updated CLAUDE.md to two-process architecture
- Restored wombles doc after data loss

## Gotchas
- ServerSession.send_message() is explicitly documented as "may change without notice" (mcp 1.26.0). An SDK upgrade could break channel notifications silently. The assertion in _send_channel_notification will catch it at runtime.
- _init_options_with_channel monkey-patches _mcp_server.create_initialization_options to inject the claude/channel capability. If FastMCP adds native experimental_capabilities support, replace the monkey-patch.
- pending_notifications list on RoomConnection is redundant now that channel delivery works, but 5 tests assert on it. Removing means rewriting tests to mock session.
- yupdates table grows unboundedly with squashing disabled. Not a problem yet but will slow room restore for heavily-edited docs.
- inspect_doc uses text.diff() (no args) not text.diff(txn) — the high-level API handles transactions internally.

## Risks
- Upstream PR may take weeks (repo dormant since Dec 2025). If squash-less persistence becomes a problem, we'd need to vendor the fix or own squashing ourselves.
- Channel notifications require --dangerously-load-development-channels server:tafelmusik flag — easy to forget on session start.
- No notification for rooms Claude hasn't touched (tfm-ditufu). Sameer editing a new room is invisible.

## Next
- Sameer has UI tweaks queued and markdown docs to test the editing grammar against
- tfm-zeguhi: implement patch mode for surgical edits — test against real docs, add scenarios to editing-grammar.md
- tfm-ditufu: room awareness (poll room list? connect-all-on-startup? ASGI push?)
- tfm-melemu: comments — not started, StickyIndex + Y.Map infrastructure exists but not wired
- tfm-neceki: images — unblocks tfm-hubipu (export)
- Re-enable squashing when pycrdt-store fix ships

## Commands
```bash
uv run pytest src/ -v                              # 67 tests
bon list                                           # Full hierarchy
bon show tfm-zeguhi                                # Editing grammar outcome
gh pr view 25 -R y-crdt/pycrdt-store               # Upstream PR status
```

## Reflection
**Claude observed:** The pycrdt-store bug was the most valuable find — it came from following an observation rather than dismissing it. The editing grammar discussion reframed the editing tools as a design problem (what operations does a Claude-human pair need?) rather than an implementation problem (what does the CRDT API support?). The five scenarios are all from real friction, not speculation.

**User noted:** Pushed for the design doc to be living and scenario-driven — capture as we use it, update from real docs. Correctly identified that the h1 replace was a symptom of a general ergonomics problem, not a one-off bug. The editing grammar approach (document scenarios from use, then design operations) matches the capture-vs-analysis pattern from last session.
