#!/usr/bin/env bash
# SessionStart hook: ensure Tafelmusik ASGI server is running.
# Designed for Claude's experience — clear errors, automatic recovery.
set -euo pipefail

PORT="${TAFELMUSIK_PORT:-3456}"
URL="http://127.0.0.1:${PORT}/"

# 1. Check uv
if ! command -v uv &>/dev/null; then
    echo "uv not found — install via: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# 2. Check .venv
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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
    echo "systemd unit not installed — start manually: cd $PLUGIN_ROOT && uv run python -m tafelmusik.asgi_server"
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
