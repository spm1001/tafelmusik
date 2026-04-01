# Notification Architecture — Exploration Record

How do we push "Sameer commented" into Claude's model context? Explored 2026-04-01.

## The problem

Sameer comments on text in the browser. Claude needs to know — without Sameer typing "check the doc" in the CLI. The comment is already in the Y.Map (CRDT syncs it). The question is delivery of the *signal* to Claude's context.

## Mechanisms explored

### 1. Channel notifications (MCP)

**How:** MCP server sends `notifications/claude/channel` via `ServerSession.send_message()`. CC should inject as a system message.

**Status:** Architecturally correct. Pipeline works end-to-end — observer fires, debouncer runs, send_message writes to stdio. But CC silently drops channel notifications in most REPL states. Known bug (anthropics/claude-code#36975, #37139, #40237), regressed in v2.1.86, unfixed as of v2.1.89. Feature is "research preview", gated behind `--dangerously-load-development-channels`, OAuth-only (no Vertex), requires KAIROS feature flag.

**Verdict:** Right design, broken delivery. Don't depend on it.

### 2. FileChanged hooks (CC built-in)

**How:** MCP server writes to a signal file. CC's chokidar watcher detects the change. FileChanged hook runs, returns `systemMessage` in JSON output.

**Status:** Plumbing works — watcher initializes, detects changes, hook runs, JSON parses. But `systemMessage` from FileChanged hooks goes to `notifyCallback` → `addNotification()` → transient UI widget (5s grey flash). **Does not inject into model context.** FileChanged hooks are designed for environment setup (`.envrc`), not model notifications.

**Key findings during investigation:**
- Matcher uses `basename(file_path)`, not full path — match against filename only
- `watchPaths` from SessionStart hooks dynamically register paths via `updateWatchPaths()`
- `initializeFileChangedWatcher` has `if (initialized) return` guard — won't re-init on resume
- `getUnixTime = Date.now` (milliseconds, not seconds) — got this wrong initially

**Verdict:** Dead end for model context. User-notification only.

### 3. Background task watcher ("mousetrap")

**How:** Start a background Bash command: `while [ ! -s signal ]; do sleep 1; done; cat signal`. When the signal file gets content, the command exits. CC sends a `<task-notification>` with the output file path. Claude reads it.

**Status:** Works. Proven end-to-end. Push delivery, no feature gates, no experimental APIs.

**Trade-offs:**
- One-shot: command exits after first signal, must be manually restarted
- Token cost: each cycle is a Bash tool call (start watcher) + task notification + Read (output file)
- 1-second polling latency (could be instant with `inotifywait`, not installed)
- No cleanup on session crash — orphaned sleep loop

**Verdict:** Working. Ugly but functional. The "mousetrap" — must be reset after each catch.

### 4. asyncRewake SessionStart hook

**How:** SessionStart hook with `asyncRewake: true` and `async: true`. Runs the same signal-file watcher as #3 but as a hook, not a manual Bash call. Exits with code 2 when signal arrives. CC's `enqueuePendingNotification` injects the stderr output into model context via `wrapInSystemReminder()`.

**Status:** Works for first notification. Proven: comment appeared as `<system-reminder>Stop hook blocking error from command "SessionStart:resume": {signal content}`. But one-shot — hook exits after first fire, CC doesn't restart it.

**Key code path:** `hooks.ts` line 236: exit code 2 → `enqueuePendingNotification({value: wrapInSystemReminder(...), mode: 'task-notification'})`. This is the only confirmed non-channel path from external event → model context.

**Verdict:** Best single-notification delivery. No manual setup. But needs mousetrap (#3) for subsequent notifications.

## Signal file format

JSONL at `~/.tafelmusik-signal`. Each line:

```json
{"room": "path/to/doc", "author": "sameer", "quote": "anchored text", "body": "comment body", "drift": 518, "ts": "2026-04-01T17:10:56"}
```

Written by `_write_signal()` in `mcp_server.py`. Read and truncated by the consumer (hook or watcher).

Known issue: duplicate entries per comment — Y.Map observer fires per-field, debouncer doesn't catch all.

## What actually injects into model context

From CC source analysis (`~/Repos/claude-code/src/`):

| Path | Injects? | How |
|------|----------|-----|
| SessionStart hook `hookMessages` | Yes | Conversation messages |
| Channel notifications | Yes (when working) | Command queue → SleepTool wake |
| `enqueuePendingNotification` | Yes | Command queue or mid-query attachment |
| `asyncRewake` hook exit code 2 | Yes | Via `enqueuePendingNotification` |
| FileChanged `systemMessage` | No | `addNotification` → transient UI only |
| FileChanged `hookSpecificOutput` | No | Same path |
| Background task completion | Yes | `<task-notification>` tag |

## Recommended approach (current)

**First notification:** asyncRewake SessionStart hook catches it for free.

**Subsequent notifications:** Background task watcher (mousetrap), restarted by Claude after each catch.

**Future:** Replace y-websocket with standalone code, implement proper awareness echo or server-side keepalive, potentially adopt CC's swarm messaging when it ships publicly.

## Related

- `docs/calute-phase2-brief.md` — original notification design (channels-based)
- `.bon/understanding.md` — "The notification delivery gap" section
- `~/Repos/claude-code/src/utils/hooks/fileChangedWatcher.ts` — CC FileChanged implementation
- `~/Repos/claude-code/src/utils/hooks.ts` line 205-246 — asyncRewake code path
- `~/Repos/batterie/aboyeur/docs/future-sketch-mesh-coordination.md` — multi-Claude vision
