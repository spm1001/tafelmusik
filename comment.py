# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Store a comment anchored to tmux selection.

Usage:
    echo "my reaction" | uv run --script comment.py [--target TARGET]

Reads the tmux buffer for the quote (the selected text).
Reads stdin for the body (the comment).
Stores in ~/.tafelmusik-comments.db.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from pathlib import Path
from tafelmusik.anchored import CommentStore, capture_context


DB_PATH = Path.home() / ".tafelmusik-comments.db"


def get_tmux_buffer() -> str | None:
    """Get the most recent tmux paste buffer."""
    try:
        result = subprocess.run(
            ["tmux", "save-buffer", "-"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_target() -> str:
    """Derive a target from args or default."""
    for i, arg in enumerate(sys.argv):
        if arg == "--target" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    # Default: current tmux session:window
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}:#{window_name}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return f"tmux/{result.stdout.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "session/unknown"


def main():
    quote = get_tmux_buffer()
    if not quote:
        print("No tmux selection found.", file=sys.stderr)
        sys.exit(1)

    # Body from stdin (piped from tmux command-prompt)
    if not sys.stdin.isatty():
        body = sys.stdin.read().strip()
    else:
        body = input("Comment: ").strip()

    if not body:
        print("Empty comment, skipping.", file=sys.stderr)
        sys.exit(1)

    target = get_target()

    store = CommentStore(DB_PATH)
    comment = store.create(
        author="sameer",
        target=target,
        body=body,
        quote=quote,
    )
    store.close()

    print(f"Stored: {comment.id} on {target}")
    print(f"Quote: {quote[:60]}{'...' if len(quote) > 60 else ''}")


if __name__ == "__main__":
    main()
