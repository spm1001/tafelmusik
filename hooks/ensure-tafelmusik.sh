#!/usr/bin/env bash
# SessionStart hook: ensure Tafelmusik ASGI server is running.
# Designed for Claude's experience — clear errors, automatic recovery.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 0. Instruction shard
if [ -f "$PLUGIN_ROOT/instructions.md" ]; then
    mkdir -p "$HOME/.claude/rules"
    ln -sf "$PLUGIN_ROOT/instructions.md" "$HOME/.claude/rules/tafelmusik.md"
fi

# 0c. Kill orphaned MCP servers whose parent claude process is gone.
# Process tree: claude → uv → python -m tafelmusik.mcp_server
# When claude dies uncleanly, uv + python may survive as orphans.
for pid in $(pgrep -f 'tafelmusik\.mcp_server' 2>/dev/null || true); do
    # Walk up: python → uv → grandparent
    ppid=$(awk '/^PPid:/{print $2}' /proc/$pid/status 2>/dev/null) || continue
    gppid=$(awk '/^PPid:/{print $2}' /proc/$ppid/status 2>/dev/null) || continue
    # If grandparent is init (1) or systemd, the original claude is gone
    if [ "$gppid" = "1" ]; then
        echo "Killing orphaned MCP server PID $pid (grandparent gone)"
        kill "$pid" 2>/dev/null || true
    fi
done

PORT="${TAFELMUSIK_PORT:-3456}"
URL="http://127.0.0.1:${PORT}/"

# 1. Check uv
if ! command -v uv &>/dev/null; then
    echo "uv not found — install via: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# 2. Check .venv
if [ ! -d "$PLUGIN_ROOT/.venv" ]; then
    echo "Installing dependencies..."
    (cd "$PLUGIN_ROOT" && uv sync --quiet)
fi

# 3. Check ASGI server
if curl -sf --max-time 2 "$URL" >/dev/null 2>&1; then
    echo '{"hookSpecificOutput": "Tafelmusik server running on :'"$PORT"'"}'
    exit 0
fi

# Server not reachable — check for stale process
STALE_PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$STALE_PID" ]; then
    echo "Killing stale process on port $PORT (PID: $STALE_PID)"
    kill "$STALE_PID" 2>/dev/null || true
    sleep 1
fi

# Start server via systemd if available
if systemctl --user is-enabled tafelmusik.service &>/dev/null; then
    systemctl --user restart tafelmusik.service
else
    echo "systemd unit not installed — start manually: cd $PLUGIN_ROOT && uv run uvicorn tafelmusik.asgi_server:app --host 0.0.0.0 --port $PORT"
    exit 1
fi

# Wait for server
for i in 1 2 3 4 5; do
    if curl -sf --max-time 1 "$URL" >/dev/null 2>&1; then
        echo '{"hookSpecificOutput": "Tafelmusik server started on :'"$PORT"'"}'
        exit 0
    fi
    sleep 1
done

echo "Tafelmusik server failed to start on port $PORT after 5s"
exit 1
