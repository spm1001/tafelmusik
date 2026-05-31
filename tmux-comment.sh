#!/bin/bash
# Tmux popup for anchored comments.
# Bind in .tmux.conf:   bind C-n run-shell "~/.local/opt/tafelmusik/tmux-comment.sh"
#
# Flow: select text in tmux → prefix+Ctrl+N → popup → type comment → enter → stored.
#
# Session ID is resolved HERE (in the original pane context) and passed
# to comment.py via --session-id. The popup runs in a new pane where
# the pane-aware Claude PID lookup would find nothing.

# Self-locating: comment.py is this script's sibling. readlink -f resolves the
# ~/.local/opt/tafelmusik indirection symlink to the real repo, so this works
# no matter where the repo lives (infra iw-vakuwa — no hardcoded worktree path).
SCRIPT="$(dirname "$(readlink -f "$0")")/comment.py"

# Resolve session ID from the active pane's Claude process
SESSION_ID=""
PANE_PID=$(tmux display-message -p '#{pane_pid}')
if [ -n "$PANE_PID" ]; then
    CLAUDE_PID=$(pgrep -P "$PANE_PID" -a 2>/dev/null | grep claude | grep -v grep | head -1 | awk '{print $1}')
    if [ -n "$CLAUDE_PID" ] && [ -f "$HOME/.claude/sessions/${CLAUDE_PID}.json" ]; then
        SESSION_ID=$(python3 -c "import json; print(json.load(open('$HOME/.claude/sessions/${CLAUDE_PID}.json')).get('sessionId',''))" 2>/dev/null)
    fi
fi

if [ -n "$SESSION_ID" ]; then
    tmux display-popup -E -w 80 -h 4 -T " Comment " \
        "read -e -p '> ' body && echo \"\$body\" | uv run --script $SCRIPT --session-id $SESSION_ID"
else
    tmux display-popup -E -w 80 -h 4 -T " Comment " \
        "read -e -p '> ' body && echo \"\$body\" | uv run --script $SCRIPT"
fi
