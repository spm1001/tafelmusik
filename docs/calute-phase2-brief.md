# Calute Phase 2: Implementation Brief

Referenced from tfm-calute. This is the *how* — sequencing, coordination constraints, gotchas. The bons (tfm-semame, tfm-nocaga, tfm-napari) carry the *what*.

## The design in one paragraph

Claude never chooses to read a document. The system pushes content at three moments: (1) room connect = full doc, (2) comment arrives + high drift = full doc alongside comment, (3) idle after major surgery + high drift = full resync. When drift is low and a comment arrives, Claude gets just the comment — it already knows the doc from writing it or from prior pushes. Document-body edits from Sameer produce silence. Comments are the collaboration protocol.

## Implementation order matters

```
tfm-becitu  →  tfm-semame  →  tfm-nocaga  →  tfm-napari
(prereq)       (observer)     (drift)        (payoff)
```

**Why this order:** If you swap the observer (semame) before drift tracking works (nocaga), Claude goes deaf to major surgery — Sameer restructures the doc, no comment, no notification, stale mental model, failed patches. Drift tracking must be ready *before* doc-change notifications go away.

And read_doc (napari) must be the *last* thing removed — it's the safety net while the push model is being validated. Remove it only after the full loop works end-to-end.

**The dangerous partial state:** observer swapped + no drift tracking = Claude only hears comments, never gets resync after major surgery. This is worse than the current system.

## What changes where

| File | Change | Action |
|------|--------|--------|
| `src/tafelmusik/comments.py` | Move add_comment, list_comments, resolve_comment from mcp_server.py | tfm-becitu |
| `src/tafelmusik/mcp_server.py` | Add Y.Map 'comments' observer, remove/gate Y.Text observer | tfm-semame |
| `src/tafelmusik/mcp_server.py` | State vector snapshot per room, drift computation, push triggers | tfm-nocaga |
| `src/tafelmusik/mcp_server.py` | Remove read_doc from @mcp.tool() | tfm-napari |
| Channel notification format | Currently: `"Modified section: ## Foo"`. New: comment payload OR full doc push | tfm-semame + tfm-nocaga |

## The observer swap (tfm-semame)

Current observer in mcp_server.py watches Y.Text 'content' via `doc.events()`. Fires on every remote mutation, debounces 2s, sends section-diff notification.

New observer watches Y.Map 'comments'. Detects new keys (comment added) or changed values (comment edited/resolved). Fires notification with:

```
{
  "type": "comment",
  "room": "meeting/standup",
  "author": "sameer",
  "quote": "the quick brown fox",
  "body": "change to 'fast'",
  "comment_id": "abc123"
}
```

The doc-change observer doesn't disappear immediately — it gets gated behind the drift threshold (see nocaga). Low drift = silence. High drift + idle = resync push. This is the transition: both observers run, but the doc-change one only fires under specific conditions.

## Drift score (tfm-nocaga)

```python
# On each full push (room connect, high-drift resync):
state.room_snapshots[room] = doc.get_state()

# On each remote edit (in the existing observer, before gating):
drift = len(doc.get_update(state.room_snapshots.get(room, b"")))

# Decision logic:
if comment_arrived:
    if drift > THRESHOLD:
        push(full_doc + comment)
        state.room_snapshots[room] = doc.get_state()  # reset
    else:
        push(comment_only)
elif drift > THRESHOLD and idle_seconds > 30:
    push(full_doc)
    state.room_snapshots[room] = doc.get_state()  # reset
# else: silence
```

**THRESHOLD starting point:** 1024 bytes of update data. This is conservative — a few paragraphs of editing. Tune based on real usage. Too low = wasted resyncs. Too high = stale model + failed patches.

**Where snapshots live:** On `AppState` in mcp_server.py, keyed by room name. `dict[str, bytes]`. Lost on MCP server restart — fine, because restart = fresh session = full push on room connect anyway.

## The idle timer

Needed for the "major surgery without commenting" case. Sameer restructures heavily, doesn't comment, walks away. Drift is high. After 30-60s of no edits, push resync.

Implementation: reset a per-room `asyncio.TimerHandle` on each remote edit. When it fires, check drift > THRESHOLD. If yes, push. If no, do nothing.

**Don't use the debouncer for this.** The debouncer is for rapid-fire edits (coalesce noise). The idle timer is for detecting quiescence after sustained editing. Different timescales, different purposes.

## Channel notification format changes

Current format (string):
```
Document 'room' edited by Sameer:
Modified section: ## Results
```

New formats:

**Comment notification** (low drift):
```
Comment on 'room' by Sameer:
> "the quick brown fox"
change to 'fast'
```

**Comment + full doc** (high drift):
```
Comment on 'room' by Sameer:
> "the quick brown fox"
change to 'fast'

[Full document content follows — your model was stale]

# Document Title
...full content...
```

**Resync** (idle after surgery, no comment):
```
Document 'room' resync — significant edits since last push:

# Document Title
...full content...
```

## Gotchas the implementing Claude should know

- **Y.Map observer vs Y.Text observer:** Both use `doc.events()` but you need to distinguish which sub-document changed. The event carries the update as bytes — you may need to inspect which Y type was modified. Alternatively, observe the Y.Map directly via `comments_map.observe()` (synchronous callback, use queue pattern like the existing observer).
- **Comment detection:** Y.Map doesn't have a built-in "new key added" event. You compare keys before/after, or observe deep changes. The existing `comments_map.observe()` callback pattern from the re-anchoring code may be relevant.
- **Origin filtering still matters.** Claude adds comments too (add_comment tool). The observer must filter by `txn.origin` to avoid self-notification on Claude's own comments, just like the current doc-change observer filters Claude's own edits.
- **Two observers running during transition.** The doc-change observer becomes the drift tracker (fires on every edit, updates drift score, but only pushes under threshold + idle conditions). The comment observer is new and fires on comment events. Both coexist — they're not mutually exclusive.
- **State vector is bytes.** `doc.get_state()` returns `bytes`. `doc.get_update(state_bytes)` returns `bytes`. `len()` on the update bytes is the drift score. No deserialization needed.
- **Room connect push:** The room poller already connects to active rooms. On connection (after SYNC_STEP2 received = synced), push the full doc content as a channel notification. This is the "initial context" push — every new room connection starts Claude with the full picture.

## What success looks like

A session where:
1. Claude connects to a room, gets full doc push (knows the content)
2. Sameer types for 5 minutes — Claude gets zero notifications (silence)
3. Sameer adds a comment "fix this typo" on "teh" — Claude gets comment-only notification, patches "teh" → "the", resolves comment
4. Sameer restructures half the document (high drift) then comments "what do you think?" — Claude gets full doc + comment, resyncs, responds
5. Sameer does major surgery then walks away (no comment) — after 30s idle, Claude gets resync push
6. `read_doc` is not available as a tool. Claude never needed it.
