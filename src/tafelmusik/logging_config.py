"""Logging configuration for tafelmusik.

Two output streams, three sinks:

1. **stderr** — human-readable, timestamped. journalctl captures the ASGI
   server's stderr; CC captures the MCP server's. Both get the same format.

2. **JSONL logs** — machine-readable, rotated. One directory
   (``~/.local/share/tafelmusik/``), two files:

   - ``tools.jsonl`` — MCP tool calls (op, room, duration, ok/error)
   - ``server.jsonl`` — ASGI lifecycle events (event, room, details)

   Room name is the correlation key. Merge with::

       cat ~/.local/share/tafelmusik/*.jsonl | jq -s 'sort_by(.ts)[]'

   Two files because two processes can't safely share a RotatingFileHandler
   (rotation renames the file under the other process's open handle).

Usage::

    from tafelmusik.logging_config import configure_logging

    # Both processes, at startup:
    configure_logging()

    # MCP server only:
    from tafelmusik.logging_config import configure_call_logging
    configure_call_logging()

    # ASGI server only:
    from tafelmusik.logging_config import configure_event_logging
    configure_event_logging()
"""

import json
import logging
import logging.handlers
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_LOG_DIR = Path.home() / ".local" / "share" / "tafelmusik"

# Dedicated loggers for JSONL records — file only, never stderr.
_calls_logger = logging.getLogger("tafelmusik.calls")
_calls_logger.propagate = False

_events_logger = logging.getLogger("tafelmusik.events")
_events_logger.propagate = False


def configure_logging(level: str = "INFO") -> None:
    """Configure stderr handler with timestamps for all tafelmusik loggers.

    Safe to call multiple times — only adds one handler.
    Call at process startup (asgi_server.py and mcp_server.py entry points).
    """
    root = logging.getLogger("tafelmusik")
    root.setLevel(getattr(logging, level.upper()))

    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(handler)


def _configure_jsonl_logger(
    logger: logging.Logger, filename: str,
) -> Path | None:
    """Wire a RotatingFileHandler to a JSONL logger. Returns file path."""
    filepath = _LOG_DIR / filename
    if logger.handlers:
        return filepath

    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    handler = logging.handlers.RotatingFileHandler(
        filepath, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return filepath


def configure_call_logging() -> Path | None:
    """Wire JSONL handler for MCP tool calls → tools.jsonl. Call once at MCP startup."""
    return _configure_jsonl_logger(_calls_logger, "tools.jsonl")


def configure_event_logging() -> Path | None:
    """Wire JSONL handler for ASGI lifecycle events → server.jsonl. Call once at ASGI startup."""
    return _configure_jsonl_logger(_events_logger, "server.jsonl")


def _write_jsonl(logger: logging.Logger, record: dict[str, Any]) -> None:
    """Write a single JSON line if the logger has handlers."""
    if logger.handlers:
        logger.info(json.dumps(record, default=str))


def log_tool_call(
    op: str,
    room: str,
    *,
    duration_ms: float | None = None,
    ok: bool = True,
    error: str | None = None,
    result_summary: str | None = None,
) -> None:
    """Write a structured JSONL record for an MCP tool invocation."""
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "op": op,
        "room": room,
    }
    if duration_ms is not None:
        record["dur_ms"] = round(duration_ms)
    if not ok:
        record["ok"] = False
        if error:
            record["error"] = error
    if result_summary:
        record["result"] = result_summary
    _write_jsonl(_calls_logger, record)


def log_tool_exception(op: str, room: str, duration_ms: float) -> None:
    """Write a JSONL error record from an active except block."""
    log_tool_call(op, room, duration_ms=duration_ms, ok=False,
                  error=traceback.format_exc()[:200])


def log_event(
    event: str,
    room: str = "",
    **details: Any,
) -> None:
    """Write a structured JSONL record for an ASGI lifecycle event.

    Args:
        event: What happened (room_created, client_connected, hydrated, etc.)
        room: Room name — the correlation key.
        **details: Additional key-value pairs included in the record.
    """
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
    }
    if room:
        record["room"] = room
    if details:
        record.update(details)
    _write_jsonl(_events_logger, record)
