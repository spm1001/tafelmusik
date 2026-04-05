"""Tests for logging_config — handler setup and JSONL format."""

import json
import logging
from unittest.mock import patch

from tafelmusik.logging_config import (
    configure_call_logging,
    configure_event_logging,
    configure_logging,
    log_event,
    log_tool_call,
)


def test_configure_logging_adds_stderr_handler():
    """configure_logging adds exactly one handler to the tafelmusik logger."""
    logger = logging.getLogger("tafelmusik")
    original_handlers = logger.handlers[:]
    try:
        logger.handlers.clear()
        configure_logging()
        assert len(logger.handlers) == 1
        handler = logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        # Verify format includes timestamp and level
        fmt = handler.formatter._fmt
        assert "%(asctime)s" in fmt
        assert "%(levelname)s" in fmt
        assert "%(name)s" in fmt
    finally:
        logger.handlers[:] = original_handlers


def test_configure_logging_idempotent():
    """Calling configure_logging twice doesn't add duplicate handlers."""
    logger = logging.getLogger("tafelmusik")
    original_handlers = logger.handlers[:]
    try:
        logger.handlers.clear()
        configure_logging()
        configure_logging()
        assert len(logger.handlers) == 1
    finally:
        logger.handlers[:] = original_handlers


def test_configure_call_logging_creates_file(tmp_path):
    """configure_call_logging creates the JSONL file handler."""
    calls_logger = logging.getLogger("tafelmusik.calls")
    original_handlers = calls_logger.handlers[:]
    try:
        calls_logger.handlers.clear()
        with patch("tafelmusik.logging_config._LOG_DIR", tmp_path):
            result = configure_call_logging()
            assert result == tmp_path / "tools.jsonl"
            assert len(calls_logger.handlers) == 1
    finally:
        calls_logger.handlers[:] = original_handlers


def test_configure_event_logging_creates_file(tmp_path):
    """configure_event_logging creates the JSONL file handler."""
    events_logger = logging.getLogger("tafelmusik.events")
    original_handlers = events_logger.handlers[:]
    try:
        events_logger.handlers.clear()
        with patch("tafelmusik.logging_config._LOG_DIR", tmp_path):
            result = configure_event_logging()
            assert result == tmp_path / "server.jsonl"
            assert len(events_logger.handlers) == 1
    finally:
        events_logger.handlers[:] = original_handlers


def test_log_tool_call_writes_valid_jsonl(tmp_path):
    """log_tool_call writes one parseable JSON line per call."""
    calls_logger = logging.getLogger("tafelmusik.calls")
    original_handlers = calls_logger.handlers[:]
    try:
        calls_logger.handlers.clear()
        with patch("tafelmusik.logging_config._LOG_DIR", tmp_path):
            configure_call_logging()

            log_tool_call("edit_doc", "test/room", duration_ms=42.7, ok=True,
                          result_summary="Replaced section ## Foo")
            log_tool_call("flush_doc", "test/room", duration_ms=150.0, ok=False,
                          error="git commit failed")

            for h in calls_logger.handlers:
                h.flush()

            lines = (tmp_path / "tools.jsonl").read_text().strip().split("\n")
            assert len(lines) == 2

            rec1 = json.loads(lines[0])
            assert rec1["op"] == "edit_doc"
            assert rec1["room"] == "test/room"
            assert rec1["dur_ms"] == 43  # rounded
            assert "ts" in rec1
            assert rec1["result"] == "Replaced section ## Foo"
            assert "ok" not in rec1  # ok=True is omitted (compact)

            rec2 = json.loads(lines[1])
            assert rec2["ok"] is False
            assert rec2["error"] == "git commit failed"
    finally:
        calls_logger.handlers[:] = original_handlers


def test_log_event_writes_valid_jsonl(tmp_path):
    """log_event writes lifecycle events to server.jsonl."""
    events_logger = logging.getLogger("tafelmusik.events")
    original_handlers = events_logger.handlers[:]
    try:
        events_logger.handlers.clear()
        with patch("tafelmusik.logging_config._LOG_DIR", tmp_path):
            configure_event_logging()

            log_event("client_connected", "docs/foo", clients=3)
            log_event("room_evicted", "docs/bar")

            for h in events_logger.handlers:
                h.flush()

            lines = (tmp_path / "server.jsonl").read_text().strip().split("\n")
            assert len(lines) == 2

            rec1 = json.loads(lines[0])
            assert rec1["event"] == "client_connected"
            assert rec1["room"] == "docs/foo"
            assert rec1["clients"] == 3
            assert "ts" in rec1

            rec2 = json.loads(lines[1])
            assert rec2["event"] == "room_evicted"
            assert rec2["room"] == "docs/bar"
    finally:
        events_logger.handlers[:] = original_handlers


def test_calls_logger_does_not_propagate():
    """JSONL records don't leak to stderr via the parent logger."""
    assert logging.getLogger("tafelmusik.calls").propagate is False


def test_events_logger_does_not_propagate():
    """JSONL records don't leak to stderr via the parent logger."""
    assert logging.getLogger("tafelmusik.events").propagate is False
