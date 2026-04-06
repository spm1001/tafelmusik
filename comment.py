# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Post a comment to the Tafelmusik server, anchored to tmux selection.

Usage:
    echo "my reaction" | uv run --script comment.py [--target ROOM]

Reads the tmux buffer for the quote (the selected text).
Reads stdin for the body (the comment).
POSTs to the Tafelmusik ASGI server's comment endpoint.

If --target is not specified, auto-discovers the active room from the server.
Server URL from TAFELMUSIK_URL env var (default: http://hezza:3456).
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def _server_url() -> str:
    """Get HTTP base URL from TAFELMUSIK_URL env var."""
    url = os.environ.get("TAFELMUSIK_URL", "http://hezza:3456")
    return url.replace("ws://", "http://").replace("wss://", "https://")


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


def get_session_id() -> str | None:
    """Get CC session ID for the Claude running in the current tmux pane.

    Walks: tmux pane_pid → shell PID → Claude child PID → session file.
    Falls back to most recent session file if not in tmux.
    """
    session_dir = os.path.expanduser("~/.claude/sessions")
    if not os.path.isdir(session_dir):
        return None

    # Try pane-aware lookup first
    claude_pid = _find_claude_pid_in_pane()
    if claude_pid:
        session_file = os.path.join(session_dir, f"{claude_pid}.json")
        try:
            with open(session_file) as f:
                return json.load(f).get("sessionId")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # Fallback: most recent session file
    try:
        files = [
            os.path.join(session_dir, f)
            for f in os.listdir(session_dir)
            if f.endswith(".json")
        ]
        if not files:
            return None
        latest = max(files, key=os.path.getmtime)
        with open(latest) as f:
            return json.load(f).get("sessionId")
    except Exception:
        return None


def _find_claude_pid_in_pane() -> str | None:
    """Find the Claude process PID in the active tmux pane."""
    try:
        # Get the shell PID of the active pane
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        pane_pid = result.stdout.strip()

        # Find claude child of that shell
        result = subprocess.run(
            ["pgrep", "-P", pane_pid, "-a"],
            capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.splitlines():
            if "claude" in line and "grep" not in line:
                return line.split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _get_arg(flag: str) -> str | None:
    """Get a named argument value from sys.argv."""
    for i, arg in enumerate(sys.argv):
        if arg == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def get_target() -> str | None:
    """Get room name from --target arg or auto-discover active room."""
    explicit = _get_arg("--target")
    if explicit:
        return explicit
    # Auto-discover: query server for active rooms
    try:
        url = f"{_server_url()}/api/rooms"
        response = urllib.request.urlopen(url, timeout=2)
        data = json.loads(response.read())
        rooms = data.get("rooms", [])
        active = [r["name"] for r in rooms if r.get("active")]
        if active:
            if len(active) > 1:
                print(f"Multiple active rooms, using: {active[0]}", file=sys.stderr)
            return active[0]
    except Exception:
        pass
    return None


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

    session_id = _get_arg("--session-id") or get_session_id()
    target = get_target()
    base_url = _server_url()

    comment_payload = json.dumps({
        "author": "sameer",
        "body": body,
        "quote": quote,
    }).encode()

    headers = {"Content-Type": "application/json"}
    routed_via = None

    # Try session-direct first (reaches specific Claude in this pane)
    if session_id:
        url = f"{base_url}/api/sessions/{session_id}/comments"
        req = urllib.request.Request(url, data=comment_payload, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(req, timeout=5)
            result = json.loads(response.read())
            routed_via = "session"
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"Failed to post: {e}", file=sys.stderr)
                sys.exit(1)
            # 404 = session not connected, fall through to room

    # Fall back to room endpoint (broadcast to all peers in room)
    if routed_via is None:
        if not target:
            print("No session connected and no active room found.", file=sys.stderr)
            sys.exit(1)
        url = f"{base_url}/api/rooms/{target}/comments"
        req = urllib.request.Request(url, data=comment_payload, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(req, timeout=5)
            result = json.loads(response.read())
            routed_via = "room"
        except urllib.error.URLError as e:
            print(f"Failed to post: {e}", file=sys.stderr)
            sys.exit(1)

    route_label = f"→ session" if routed_via == "session" else f"→ room:{target}"
    print(f"Posted: {result['id']} {route_label}")
    print(f"Quote: {quote[:60]}{'...' if len(quote) > 60 else ''}")


if __name__ == "__main__":
    main()
