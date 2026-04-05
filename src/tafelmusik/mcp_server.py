"""MCP server entry point — ephemeral, connects to ASGI server as Yjs peer.

Architecture: FastMCP with stdio transport. Maintains persistent WebSocket
connections to the ASGI server — one per room. Each connection runs a
standalone Yjs sync loop that syncs a local Doc with the server's Y.Doc.
Tools operate on the local Doc; the sync loop handles bidirectional sync.

Lifecycle: httpx client created at startup (lifespan), room connections
created lazily on first tool call, all cleaned up when the MCP process exits.

Important: aconnect_ws (httpx-ws) uses anyio cancel scopes that are bound to
the asyncio Task that entered them. The sync loop must run in the SAME task
that opens the WebSocket. This is why _sync_task() wraps both aconnect_ws
and _sync_loop() — separating them across tasks causes cancel scope errors.
"""

from __future__ import annotations

import asyncio
import json as json_mod
import logging
import os
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from anyio import Event, create_task_group, sleep
from httpx_ws import aconnect_ws
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from pycrdt import (
    Doc,

    Text,
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)

from tafelmusik import authors, document
from tafelmusik.logging_config import (
    configure_call_logging,
    configure_logging,
    log_tool_call,
    log_tool_exception,
)

log = logging.getLogger(__name__)

# Drift threshold in bytes of CRDT update data. When the binary diff between
# the last-pushed state vector and the current Doc exceeds this, Claude's
# mental model is considered stale and a full document push is triggered.
# Conservative starting point — a few paragraphs of editing. Tune based on
# real usage: too low = wasted resync tokens, too high = stale model + failed patches.
DRIFT_THRESHOLD = 1024

# Seconds of no edits before a high-drift resync is pushed automatically.
# Detects the "major surgery without commenting" case — Sameer restructures
# heavily, doesn't comment, walks away. Separate from the comment debouncer
# (different timescale, different purpose).
IDLE_TIMEOUT = 30.0


# --- Tool logging helpers ---
# Room name is the correlation key between MCP (tools.jsonl) and ASGI
# (server.jsonl) logs. Both live in ~/.local/share/tafelmusik/.


def _log_tool_entry(op: str, room: str = "?", **extras: object) -> float:
    """Log tool entry, return monotonic timestamp for duration."""
    parts = [f"op={op}", f"room={room}"]
    parts.extend(f"{k}={v}" for k, v in extras.items() if v is not None)
    log.info("%s", " ".join(parts))
    return time.monotonic()


def _log_tool_result(
    op: str, room: str, t0: float, result: str, *, error: bool = False,
) -> None:
    """Log tool completion to stderr + JSONL call log."""
    dur = (time.monotonic() - t0) * 1000
    level = logging.WARNING if error else logging.INFO
    preview = result[:100].replace("\n", " ")
    log.log(level, "op=%s room=%s dur=%.0fms status=%s %s",
            op, room, dur, "error" if error else "ok", preview)
    log_tool_call(
        op, room, duration_ms=dur, ok=not error,
        error=preview if error else None,
        result_summary=preview if not error else None,
    )


def _log_tool_error(op: str, room: str, t0: float) -> None:
    """Log uncaught tool exception. Call from except block before re-raising."""
    dur = (time.monotonic() - t0) * 1000
    log.warning("op=%s room=%s dur=%.0fms status=error", op, room, dur,
                exc_info=True)
    log_tool_exception(op, room, dur)


def _ws_to_http(url: str) -> str:
    """Convert ws:// or wss:// URL to http:// or https:// for httpx."""
    return url.replace("ws://", "http://").replace("wss://", "https://")


# --- Channel abstraction ---


class Channel(Protocol):
    """Bidirectional message channel for the Yjs sync protocol."""

    async def send(self, message: bytes) -> None: ...
    def __aiter__(self) -> Channel: ...
    async def __anext__(self) -> bytes: ...


class WebSocketChannel:
    """Wraps an httpx-ws session as a Yjs sync channel."""

    def __init__(self, websocket) -> None:
        self._ws = websocket

    async def send(self, message: bytes) -> None:
        # httpx-ws already serialises writes via its internal _write_lock
        await self._ws.send_bytes(message)

    def __aiter__(self) -> WebSocketChannel:
        return self

    async def __anext__(self) -> bytes:
        try:
            return bytes(await self._ws.receive_bytes())
        except Exception:
            raise StopAsyncIteration()


# --- Standalone Yjs sync protocol ---


async def _send_updates(doc: Doc, channel: Channel) -> None:
    """Broadcast local Doc changes to the peer as SYNC_UPDATE messages."""
    async with doc.events() as events:
        async for event in events:
            await channel.send(create_update_message(event.update))


async def _sync_loop(
    doc: Doc,
    channel: Channel,
    synced: Event,
    *,
    keepalive: float | None = 60.0,
    on_comment: Callable[[dict], None] | None = None,
) -> None:
    """Run the Yjs sync protocol over a channel.

    Sends SYNC_STEP1, handles incoming sync messages, and broadcasts local
    changes. Sets ``synced`` when SYNC_STEP2 is received (server's full state
    has been applied). Returns when the channel closes or keepalive times out.

    Args:
        keepalive: Seconds of silence before sending a sync probe. If the
            probe gets no response within ``min(keepalive, 10)`` more seconds,
            the connection is presumed dead and the loop exits. ``None``
            disables keepalive (used in tests).
    """
    await channel.send(create_sync_message(doc))
    last_recv = time.monotonic()
    # Capture as non-optional float so _heartbeat's type is clean.
    # Guarded by `if keepalive is not None` before start_soon(_heartbeat).
    interval = keepalive or 0.0

    async def _heartbeat() -> None:
        while True:
            await sleep(interval)
            if time.monotonic() - last_recv < interval:
                continue
            # No messages received — send a sync probe
            try:
                await channel.send(create_sync_message(doc))
            except Exception:
                tg.cancel_scope.cancel()
                return
            # Wait for a response
            await sleep(min(interval, 10.0))
            if time.monotonic() - last_recv >= interval:
                log.warning(
                    "No messages in %.0fs (keepalive probe unanswered), connection presumed dead",
                    time.monotonic() - last_recv,
                )
                tg.cancel_scope.cancel()
                return

    # _send_updates and _heartbeat run as anyio task group subtasks (separate
    # asyncio Tasks).  This is safe because they only do I/O (send_bytes) on
    # the WebSocket — they don't enter/exit cancel scopes.  The constraint
    # "same asyncio Task" applies to cancel scope lifecycle (aconnect_ws
    # __aenter__/__aexit__), not to I/O within an existing scope.  Structured
    # concurrency guarantees subtasks are cancelled before the parent's scope
    # exits, so no subtask can touch the WebSocket after aconnect_ws closes.
    async with create_task_group() as tg:
        tg.start_soon(_send_updates, doc, channel)
        if keepalive is not None:
            tg.start_soon(_heartbeat)
        async for message in channel:
            last_recv = time.monotonic()
            if message[0] == YMessageType.SYNC:
                msg_type = YSyncMessageType(message[1])
                reply = handle_sync_message(message[1:], doc)
                if reply is not None:
                    await channel.send(reply)
                if msg_type == YSyncMessageType.SYNC_STEP2 and not synced.is_set():
                    synced.set()
            elif message[0] == 0x01 and on_comment is not None:
                # Comment event — type prefix from asgi_server.COMMENT_MSG_TYPE
                try:
                    event = json_mod.loads(message[1:])
                    on_comment(event)
                except Exception:
                    log.warning("Failed to parse comment event", exc_info=True)
        # Channel iterator ended — connection lost. Cancel _send_updates
        # so this function returns and the caller can set the dead event.
        tg.cancel_scope.cancel()


# --- Channel notifications ---


async def _send_channel_notification(
    session: ServerSession,
    content: str,
    *,
    meta: dict | None = None,
) -> None:
    """Send a channel notification to Claude Code.

    Uses the low-level ServerSession.send_message() to send a custom
    JSONRPCNotification — the typed send_notification() API doesn't support
    the notifications/claude/channel method.

    Private API: ServerSession.send_message() (mcp 1.26.0)
    Validated: session.py:669 — experimental, documented as "may change"
    """
    assert hasattr(session, "send_message"), (
        "ServerSession.send_message() not found — MCP SDK API may have changed"
    )

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta or {}},
    )
    msg = SessionMessage(message=JSONRPCMessage(notification))
    await session.send_message(msg)


def _compute_drift(conn: RoomConnection, room: str) -> int:
    """Compute drift score: bytes of CRDT updates since last snapshot."""
    if conn._app_state is None:
        return 0
    snapshot = conn._app_state.room_snapshots.get(room, b"\x00")
    return len(conn.doc.get_update(snapshot))


def _reset_snapshot(conn: RoomConnection, room: str) -> None:
    """Capture current state vector as the new baseline for drift."""
    if conn._app_state is not None:
        conn._app_state.room_snapshots[room] = conn.doc.get_state()


async def _push_resync(conn: RoomConnection, room: str) -> None:
    """Push full document content as a resync notification if drift is high.

    Called by the idle timer after IDLE_TIMEOUT seconds of no remote edits.
    Checks drift before pushing — if edits were minor, no push needed.
    """
    if conn._app_state is None or conn._app_state.session is None:
        return
    drift = _compute_drift(conn, room)
    log.info(
        "Room %s: idle timer fired — drift %dB (threshold %dB, %s)",
        room, drift, DRIFT_THRESHOLD, "pushing" if drift > DRIFT_THRESHOLD else "skipping",
    )
    if drift <= DRIFT_THRESHOLD:
        return
    content = (
        f"Document '{room}' resync — significant edits since last push:\n\n"
        + str(conn.text)
    )
    try:
        await _send_channel_notification(
            conn._app_state.session,
            content,
            meta={"room": room, "type": "resync", "drift": drift},
        )
        _reset_snapshot(conn, room)
        log.info("Room %s: resync pushed (idle after high drift, %dB)", room, drift)
    except Exception:
        log.warning("Failed to send resync for room %s", room, exc_info=True)


async def _comment_consumer(
    conn: RoomConnection,
    room: str,
    debounce: float = 0.5,
) -> None:
    """Consume comment events, debounce, and send drift-aware notifications.

    Comments are discrete events (not rapid-fire keystroke streams), so
    debounce is shorter than the old doc-change observer (0.5s vs 2s).

    When drift is high (model stale from Sameer's edits), the notification
    includes the full document content alongside the comment. When drift is
    low (Claude already knows the doc), comment-only is sent.
    """
    while True:
        first = await conn._comment_queue.get()
        events = [first]

        # Drain additional comments within the debounce window
        while True:
            try:
                more = await asyncio.wait_for(
                    conn._comment_queue.get(), timeout=debounce
                )
                events.append(more)
            except TimeoutError:
                break

        if conn._app_state is None:
            continue

        # Wait for session — captured at initialization via _handle_message
        # wrapper, or on first tool call via _get_state. Comments arriving
        # before session capture queue here instead of being dropped.
        if conn._app_state.session is None:
            try:
                await asyncio.wait_for(
                    conn._app_state.session_ready.wait(),
                    timeout=conn._app_state.session_timeout,
                )
            except TimeoutError:
                log.warning(
                    "Room %s: %d comment(s) dropped — no MCP session after %.0fs",
                    room,
                    len(events),
                    conn._app_state.session_timeout,
                )
                continue

        drift = _compute_drift(conn, room)
        high_drift = drift > DRIFT_THRESHOLD
        log.info(
            "Room %s: comment notification — drift %dB (%s, threshold %dB)",
            room, drift, "high" if high_drift else "low", DRIFT_THRESHOLD,
        )

        for event in events:
            comment_text = (
                f"Comment on '{room}' by {event['author']}:\n"
                f"> \"{event['quote']}\"\n"
                f"{event['body']}"
            )
            if high_drift:
                content = (
                    comment_text
                    + "\n\n[Full document content follows — your model was stale]\n\n"
                    + str(conn.text)
                )
                ntype = "comment+resync"
            else:
                content = comment_text
                ntype = "comment"
            try:
                await _send_channel_notification(
                    conn._app_state.session,
                    content,
                    meta={
                        "room": room,
                        "type": ntype,
                        "comment_id": event["comment_id"],
                        "drift": str(drift),
                    },
                )
            except Exception:
                log.warning(
                    "Failed to send comment notification for room %s",
                    room,
                    exc_info=True,
                )

        # Reset snapshot after high-drift push and cancel idle timer
        # (prevents a redundant resync if the timer fires before next edit)
        if high_drift:
            _reset_snapshot(conn, room)
            if conn._idle_timer is not None:
                conn._idle_timer.cancel()
                conn._idle_timer = None


# --- Connection management ---


@dataclass
class RoomConnection:
    """A live connection to a single room on the ASGI server.

    Holds the local Doc (with a Y.Text keyed "content") and events for
    sync status and liveness. The sync loop runs in a background task
    that also owns the WebSocket — they share the same asyncio Task to
    avoid anyio cancel scope cross-task violations.

    Two observers:
    - Text observer: resets idle timer on every remote change. Does NOT
      send notifications directly.
    - Comment handler: HTTP/0x01 comment events from the ASGI broadcast
      are routed into _comment_queue by _handle_comment_event, consumed
      by _comment_consumer for channel notifications.

    Idle timer: fires after IDLE_TIMEOUT seconds of no remote edits.
    If drift is high, pushes a full resync.
    """

    doc: Doc
    text: Text
    synced: Event
    dead: Event
    _task: asyncio.Task
    _observe_subscription: Any = None  # text observer (idle timer)
    _comment_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _comment_consumer_task: asyncio.Task | None = None
    _idle_timer: asyncio.TimerHandle | None = None
    _app_state: Any = None  # AppState, set after construction (forward ref)

    async def close(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._observe_subscription is not None:
            self.text.unobserve(self._observe_subscription)
            self._observe_subscription = None
        if self._comment_consumer_task and not self._comment_consumer_task.done():
            self._comment_consumer_task.cancel()
            try:
                await self._comment_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass


@dataclass
class AppState:
    """Shared state for the MCP server's lifetime.

    Holds the httpx client (shared across rooms) and a dict of active
    room connections. Rooms are connected lazily when a tool first
    accesses them.
    """

    client: httpx.AsyncClient
    server_url: str
    rooms: dict[str, RoomConnection] = field(default_factory=dict)
    room_snapshots: dict[str, bytes] = field(default_factory=dict)
    sync_timeout: float = 10.0
    keepalive: float | None = 60.0
    idle_timeout: float | None = IDLE_TIMEOUT
    session_ready: asyncio.Event = field(default_factory=asyncio.Event)
    session_timeout: float = 30.0
    docs_dir: Path | None = None

    def __post_init__(self) -> None:
        self._session: ServerSession | None = None

    @property
    def session(self) -> ServerSession | None:
        return self._session

    @session.setter
    def session(self, value: ServerSession | None) -> None:
        """Capture the MCP session and signal readiness.

        Set from _handle_message wrapper (early, at initialization) or
        from _get_state (fallback, on first tool call). Idempotent — only
        the first non-None assignment takes effect.
        """
        if self._session is None and value is not None:
            self._session = value
            self.session_ready.set()
            log.info("Captured MCP session for channel notifications")

    async def connect(self, room: str) -> RoomConnection:
        """Get or create a connection to a room.

        If a previous connection exists but died (server restart, network),
        it is cleaned up and a fresh one is created.
        """
        if room in self.rooms:
            conn = self.rooms[room]
            if not conn.dead.is_set():
                log.debug("op=connect room=%s cached", room)
                return conn
            # Connection died — clean up before reconnecting
            log.info("op=connect room=%s reconnecting (previous died)", room)
            await conn.close()
            del self.rooms[room]

        log.info("op=connect room=%s", room)
        t0 = time.monotonic()
        # httpx-ws uses http:// for the WebSocket upgrade
        http_url = _ws_to_http(self.server_url)

        doc = Doc()
        doc["content"] = text = Text()
        synced = Event()
        dead = Event()
        comment_queue: asyncio.Queue = asyncio.Queue()

        def _handle_comment_event(event: dict) -> None:
            """Route HTTP-created comment events into the notification queue.

            Called synchronously from _sync_loop when a 0x01 message arrives
            on the WebSocket. Only queues comment_created events from non-Claude
            authors — resolved events and Claude's own comments are filtered.
            """
            if event.get("type") != "comment_created":
                return
            comment = event.get("comment", {})
            if comment.get("author") == authors.CLAUDE:
                return
            comment_queue.put_nowait({
                "comment_id": comment.get("id", ""),
                "author": comment.get("author", "unknown"),
                "quote": comment.get("quote", ""),
                "body": comment.get("body", ""),
            })

        async def _sync_task():
            """Run WebSocket + sync protocol in the same asyncio Task.

            aconnect_ws cancel scopes are task-bound — entering the WebSocket
            in one task and running the sync loop in another causes RuntimeError.
            This function keeps both in the same task.
            """
            try:
                async with aconnect_ws(f"{http_url}/_ws/{room}", self.client) as ws:
                    channel = WebSocketChannel(ws)
                    await _sync_loop(
                        doc, channel, synced,
                        keepalive=self.keepalive,
                        on_comment=_handle_comment_event,
                    )
            except Exception:
                log.warning("Sync task for room %s failed", room, exc_info=True)
            finally:
                dead.set()

        task = asyncio.create_task(_sync_task())

        # Race synced against dead — if the connection fails before sync
        # completes, dead fires first and we fail fast instead of waiting
        # the full sync_timeout.
        synced_task = asyncio.ensure_future(synced.wait())
        dead_task = asyncio.ensure_future(dead.wait())
        try:
            done, _ = await asyncio.wait(
                [synced_task, dead_task],
                timeout=self.sync_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            synced_task.cancel()
            dead_task.cancel()

        if not synced.is_set():
            dt = (time.monotonic() - t0) * 1000
            # Check dead BEFORE cancelling — cancellation always sets dead
            # via the finally block, so checking after would always be True.
            failed_fast = dead.is_set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            if failed_fast:
                log.warning("op=connect room=%s dur=%.0fms status=unreachable", room, dt)
                raise ConnectionError(
                    f"Connection to room '{room}' failed. "
                    f"Is the Tafelmusik server running on {self.server_url}?"
                )
            log.warning("op=connect room=%s dur=%.0fms status=timeout", room, dt)
            raise TimeoutError(
                f"Sync with Tafelmusik server timed out after {self.sync_timeout}s "
                f"for room '{room}'. Is the server running and responding?"
            )

        dt = (time.monotonic() - t0) * 1000
        log.info("op=connect room=%s dur=%.0fms status=ok", room, dt)

        conn = RoomConnection(
            doc=doc,
            text=text,
            synced=synced,
            dead=dead,
            _task=task,
            _comment_queue=comment_queue,
            _app_state=self,
        )

        def _on_text_change(event, txn):
            """Synchronous observe callback — idle timer management.

            On remote edits (non-Claude), resets the idle timer. When the
            idle timer fires (no edits for IDLE_TIMEOUT), _push_resync
            runs if drift is high.

            Does NOT send notifications directly — comments are the
            collaboration protocol. The idle timer handles the "major
            surgery without commenting" case.

            Wrapped in try/except because an unhandled exception here
            would propagate as an ExceptionGroup when the transaction
            commits, crashing the sync loop.
            """
            try:
                if txn.origin != authors.CLAUDE and self.idle_timeout is not None:
                    # Reset idle timer on each remote edit
                    if conn._idle_timer is not None:
                        conn._idle_timer.cancel()
                    loop = asyncio.get_running_loop()
                    conn._idle_timer = loop.call_later(
                        self.idle_timeout,
                        lambda: asyncio.ensure_future(_push_resync(conn, room)),
                    )
            except Exception:
                log.warning("Text observer callback failed for room", exc_info=True)

        conn._observe_subscription = text.observe(_on_text_change)
        conn._comment_consumer_task = asyncio.create_task(
            _comment_consumer(conn, room)
        )

        # Snapshot state after initial sync — baseline for drift tracking
        _reset_snapshot(conn, room)

        # Push initial context if room has content and session is available
        doc_content = str(text)
        if doc_content and self.session is not None:
            try:
                await _send_channel_notification(
                    self.session,
                    f"Document '{room}' — initial content:\n\n{doc_content}",
                    meta={"room": room, "type": "initial"},
                )
                log.info("Room %s: initial context pushed (%d chars)", room, len(doc_content))
            except Exception:
                log.warning("Failed to push initial context for room %s", room, exc_info=True)

        self.rooms[room] = conn
        return conn

    async def start_room_poller(self, interval: float = 5.0) -> None:
        """Start a background task that discovers and connects to new rooms."""
        self._poll_interval = interval
        self._poll_task = asyncio.create_task(self._poll_rooms())

    async def _poll_rooms(self) -> None:
        """Poll the ASGI server for active rooms and connect to new ones.

        Only connects to rooms marked ``active`` (have clients connected on
        the server). File-only rooms are listed but not connected — avoids
        opening a WebSocket per .md file in the docs directory.
        """
        http_url = _ws_to_http(self.server_url)
        while True:
            try:
                response = await self.client.get(f"{http_url}/api/rooms")
                response.raise_for_status()
                rooms = response.json().get("rooms", [])
                for entry in rooms:
                    # Support both old format (string) and new format (dict)
                    if isinstance(entry, str):
                        name, active = entry, True
                    else:
                        name, active = entry["name"], entry.get("active", True)
                    if active and name not in self.rooms:
                        try:
                            await self.connect(name)
                            log.info("Room poller: connected to %s", name)
                        except (ConnectionError, TimeoutError):
                            log.debug("Room poller: failed to connect to %s", name)
            except Exception:
                log.debug("Room poller: server unreachable, will retry")
            await asyncio.sleep(self._poll_interval)

    async def close_all(self) -> None:
        if hasattr(self, "_poll_task"):
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for conn in self.rooms.values():
            await conn.close()
        self.rooms.clear()
        self.room_snapshots.clear()


# --- Docs directory helpers ---


def _get_docs_dir(state: AppState) -> Path:
    """Get docs_dir from ASGI server config, cached on AppState."""
    if state.docs_dir is not None:
        return state.docs_dir
    # Fall back to env var (same source the ASGI server uses)
    default = Path(__file__).resolve().parent.parent.parent / "docs"
    state.docs_dir = Path(os.environ.get("TAFELMUSIK_DOCS_DIR", default))
    return state.docs_dir


def _find_git_root(path: Path) -> Path | None:
    """Walk up from path to find a .git directory."""
    current = path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def _git_commit_flush(docs_dir: Path, md_path: Path, room: str) -> str:
    """Git add + commit for flush_doc. Synchronous — call via asyncio.to_thread."""
    repo_root = _find_git_root(docs_dir)
    if not repo_root:
        return " No git repo found — skipped commit."
    git_msg = f"flush: {room}"
    try:
        subprocess.run(
            ["git", "add", str(md_path)],
            cwd=str(repo_root), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", git_msg, "--", str(md_path)],
            cwd=str(repo_root), check=True, capture_output=True,
        )
        return " Git committed."
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            return " Git commit skipped (no changes)."
        stderr_msg = e.stderr.decode().strip() if e.stderr else f"exit {e.returncode}"
        log.warning("op=flush_doc room=%s git_error: %s", room, stderr_msg)
        return f" Git commit failed (exit {e.returncode})."


# --- FastMCP setup ---


@asynccontextmanager
async def _parent_watchdog(interval: float = 30.0) -> None:
    """Exit if the grandparent process (claude) dies.

    Process tree: claude → uv → python (this MCP server). When CC exits
    uncleanly, uv may survive and keep our stdin socketpair open — we
    never get EOF. This watchdog checks if the grandparent is still alive
    and raises SystemExit if not.

    Uses grandparent (not parent) because uv is an intermediary wrapper
    that can outlive CC.
    """
    ppid = os.getppid()  # uv
    try:
        grandparent = int(
            Path(f"/proc/{ppid}/status").read_text()
            .split("PPid:\t")[1].split("\n")[0]
        )
    except (FileNotFoundError, IndexError, ValueError):
        log.warning("Parent watchdog: can't determine grandparent PID, disabled")
        return

    log.info("Parent watchdog: tracking grandparent PID %d (interval %.0fs)", grandparent, interval)
    while True:
        await asyncio.sleep(interval)
        try:
            os.kill(grandparent, 0)  # signal 0 = existence check
        except ProcessLookupError:
            log.warning("Parent watchdog: grandparent PID %d gone, exiting", grandparent)
            os._exit(0)
        except PermissionError:
            pass  # process exists but we can't signal it — still alive


async def lifespan(app: FastMCP) -> AsyncIterator[AppState]:
    configure_logging()
    call_log = configure_call_logging()
    if call_log:
        log.info("JSONL call log: %s", call_log)
    server_url = os.environ.get("TAFELMUSIK_URL", "ws://127.0.0.1:3456")
    async with httpx.AsyncClient() as client:
        state = AppState(client=client, server_url=server_url)
        await state.start_room_poller()
        watchdog = asyncio.create_task(_parent_watchdog())
        try:
            yield state
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            await state.close_all()


mcp = FastMCP("tafelmusik", lifespan=lifespan)

# Override create_initialization_options to declare claude/channel capability.
# FastMCP doesn't expose experimental_capabilities, so we wrap the low-level method.
_original_init_options = mcp._mcp_server.create_initialization_options


def _init_options_with_channel(**kwargs):
    kwargs.setdefault("experimental_capabilities", {})
    kwargs["experimental_capabilities"]["claude/channel"] = {}
    return _original_init_options(**kwargs)


mcp._mcp_server.create_initialization_options = _init_options_with_channel


def _install_session_capture(server_instance) -> None:
    """Wrap Server._handle_message to capture ServerSession at initialization.

    The MCP SDK creates ServerSession in Server.run() and passes it to
    _handle_message on every message. The first message is the
    InitializedNotification — by capturing session there, it's available
    before any tool call. This fixes the gap where comments on new docs
    (never touched by a tool call) were silently dropped.

    Private API: Server._handle_message (mcp 1.26.0)
    Validated: server/lowlevel/server.py:685
    """
    assert hasattr(server_instance, "_handle_message"), (
        "Server._handle_message not found — MCP SDK API may have changed"
    )
    _original = server_instance._handle_message
    _captured = False

    async def _capturing(message, session, lifespan_context, raise_exceptions=False, **kwargs):
        nonlocal _captured
        if not _captured and hasattr(lifespan_context, "session_ready"):
            lifespan_context.session = session  # property setter handles signalling
            _captured = True
        return await _original(message, session, lifespan_context, raise_exceptions, **kwargs)

    server_instance._handle_message = _capturing


_install_session_capture(mcp._mcp_server)


def _get_state(ctx: Context) -> AppState:
    state = ctx.request_context.lifespan_context
    # Fallback: capture session on tool call if _handle_message wrapper
    # didn't fire (e.g., MCP SDK changed). Property setter is idempotent.
    state.session = ctx.request_context.session
    return state


# --- Tools ---


@mcp.tool()
async def edit_doc(
    room: str,
    mode: str,
    ctx: Context,
    content: str = "",
    find: str = "",
    replace: str = "",
) -> str:
    """Edit a document.

    Args:
        room: Document room name
        mode: How to apply the edit. One of:
            - "append": Add content to the end
            - "replace_all": Replace the entire document
            - "replace_section": Replace a section by heading (content must start
              with a markdown heading like "## Section Name"). Replaces everything
              from that heading to the next heading of equal or higher level.
              If the heading doesn't exist, appends a new section.
              NOT allowed on h1 headings (# Title) — use replace_all instead.
            - "patch": Content-addressed find-and-replace. Locates `find` text
              literally, replaces with `replace` text. Exactly one match required.
              Preserves authorship on surrounding text.
        content: The content to write (required for append, replace_all, replace_section)
        find: Text to find (required for patch mode)
        replace: Replacement text (required for patch mode; empty string = delete)
    """
    t0 = _log_tool_entry("edit_doc", room, mode=mode)
    try:
        state = _get_state(ctx)
        conn = await state.connect(room)

        if mode in ("append", "replace_section") and not content:
            msg = f"{mode} mode requires 'content' parameter"
            _log_tool_result("edit_doc", room, t0, msg, error=True)
            return msg

        if mode == "append":
            with conn.doc.transaction(origin=authors.CLAUDE):
                conn.text.insert(len(str(conn.text)), content, attrs={"author": authors.CLAUDE})
            msg = f"Appended {len(content)} chars to '{room}'"
            _log_tool_result("edit_doc", room, t0, msg)
            return msg
        elif mode == "replace_all":
            document.replace_all(conn.text, content, author=authors.CLAUDE)
            msg = f"Replaced all content in '{room}' ({len(content)} chars)"
            _log_tool_result("edit_doc", room, t0, msg)
            return msg
        elif mode == "replace_section":
            try:
                replaced = document.replace_section(conn.text, content, author=authors.CLAUDE)
            except ValueError as e:
                msg = str(e)
                _log_tool_result("edit_doc", room, t0, msg, error=True)
                return msg
            heading = content.split("\n", 1)[0].strip()
            verb = "Replaced existing" if replaced else "Appended new"
            msg = f"{verb} section '{heading}' in '{room}'"
            _log_tool_result("edit_doc", room, t0, msg)
            return msg
        elif mode == "patch":
            if not find:
                msg = "patch mode requires 'find' parameter"
                _log_tool_result("edit_doc", room, t0, msg, error=True)
                return msg
            try:
                document.patch(conn.text, find, replace, author=authors.CLAUDE)
            except ValueError as e:
                msg = f"Patch failed: {e}"
                _log_tool_result("edit_doc", room, t0, msg, error=True)
                return msg
            action = "Deleted" if not replace else "Patched"
            msg = f"{action} {len(find)} chars in '{room}'"
            _log_tool_result("edit_doc", room, t0, msg)
            return msg
        else:
            msg = (
                f"Unknown mode '{mode}'. "
                "Use 'append', 'replace_all', 'replace_section', or 'patch'."
            )
            _log_tool_result("edit_doc", room, t0, msg, error=True)
            return msg
    except Exception:
        _log_tool_error("edit_doc", room, t0)
        raise


@mcp.tool()
async def load_doc(room: str, markdown: str, ctx: Context) -> str:
    """Load markdown into a document, replacing any existing content.

    This is the simplest way to populate a document — it clears everything
    and writes the given markdown.

    Args:
        room: Document room name
        markdown: The full markdown content to load
    """
    t0 = _log_tool_entry("load_doc", room, chars=len(markdown))
    try:
        state = _get_state(ctx)
        conn = await state.connect(room)
        document.replace_all(conn.text, markdown, author=authors.CLAUDE)
        msg = f"Loaded {len(markdown)} chars into '{room}'"
        _log_tool_result("load_doc", room, t0, msg)
        return msg
    except Exception:
        _log_tool_error("load_doc", room, t0)
        raise


@mcp.tool()
async def list_docs(ctx: Context) -> str:
    """List documents available on the server.

    Returns room names from both active connections and persisted storage.
    """
    t0 = _log_tool_entry("list_docs")
    state = _get_state(ctx)
    http_url = _ws_to_http(state.server_url)
    try:
        response = await state.client.get(f"{http_url}/api/rooms")
        response.raise_for_status()
        data = response.json()
        rooms = data.get("rooms", [])
        if not rooms:
            msg = "No documents found."
            _log_tool_result("list_docs", "?", t0, msg)
            return msg
        lines = []
        for entry in rooms:
            if isinstance(entry, str):
                lines.append(f"  - {entry}")
            else:
                name = entry["name"]
                marker = " (active)" if entry.get("active") else ""
                lines.append(f"  - {name}{marker}")
        msg = "Documents:\n" + "\n".join(lines)
        _log_tool_result("list_docs", "?", t0, msg)
        return msg
    except httpx.HTTPError:
        msg = "Could not list documents (is the Tafelmusik server running?)"
        _log_tool_result("list_docs", "?", t0, msg, error=True)
        return msg


@mcp.tool()
async def flush_doc(room: str, ctx: Context) -> str:
    """Flush document to .md file on disk and git commit.

    Writes current Y.Text content to the .md file and commits to git.
    This is the "save" — the .md file becomes the durable artifact.
    SQLite comments are not affected by flush.

    Args:
        room: Document room name (maps to file path relative to docs_dir)
    """
    t0 = _log_tool_entry("flush_doc", room)
    try:
        state = _get_state(ctx)
        conn = await state.connect(room)

        content = str(conn.text)
        docs_dir = _get_docs_dir(state)
        md_path = (docs_dir / f"{room}.md").resolve()
        if not md_path.is_relative_to(docs_dir.resolve()):
            msg = f"Room name '{room}' escapes docs directory — rejected."
            _log_tool_result("flush_doc", room, t0, msg, error=True)
            return msg
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content)

        # Git commit — runs in a thread to avoid blocking the event loop.
        git_status = await asyncio.to_thread(
            _git_commit_flush, docs_dir, md_path, room
        )

        msg = f"Flushed {len(content)} chars to {md_path}.{git_status}"
        _log_tool_result("flush_doc", room, t0, msg)
        return msg
    except Exception:
        _log_tool_error("flush_doc", room, t0)
        raise


@mcp.tool()
async def inspect_doc(room: str, ctx: Context) -> str:
    """Inspect a document's Y.Text with formatting attributes and drift score.

    Returns the text as a sequence of attributed chunks — each chunk is a
    run of text sharing the same attrs (e.g. author). This reveals the
    CRDT layer that str(text) hides: who wrote what, and whether any
    unexpected formatting attributes are present.

    Also shows the drift score: bytes of CRDT updates since the last snapshot.
    High drift (>{DRIFT_THRESHOLD}B) means Claude's mental model may be stale.

    Args:
        room: Document room name
    """
    t0 = _log_tool_entry("inspect_doc", room)
    try:
        state = _get_state(ctx)
        conn = await state.connect(room)

        drift = _compute_drift(conn, room)
        drift_status = "stale" if drift > DRIFT_THRESHOLD else "fresh"
        header = (
            f"Document '{room}' — drift: {drift}B"
            f" ({drift_status}, threshold: {DRIFT_THRESHOLD}B)"
        )

        chunks = conn.text.diff()
        if not chunks:
            msg = f"{header}\n(empty)"
            _log_tool_result("inspect_doc", room, t0, msg)
            return msg
        lines = []
        for content, attrs in chunks:
            preview = repr(content) if len(content) <= 80 else repr(content[:77] + "...")
            if attrs:
                lines.append(f"  {preview}  attrs={attrs}")
            else:
                lines.append(f"  {preview}")
        msg = f"{header}\n{len(chunks)} chunk(s):\n" + "\n".join(lines)
        _log_tool_result("inspect_doc", room, t0, msg)
        return msg
    except Exception:
        _log_tool_error("inspect_doc", room, t0)
        raise


@mcp.tool()
async def add_comment(room: str, quote: str, body: str, ctx: Context) -> str:
    """Add a comment anchored to specific text in a document.

    Posts to the ASGI server's comment API. The comment is stored in SQLite
    and broadcast to all connected peers (including the browser).

    Args:
        room: Document room name
        quote: Exact text to comment on (must exist in the document)
        body: The comment text
    """
    t0 = _log_tool_entry("add_comment", room)
    try:
        state = _get_state(ctx)
        http_url = _ws_to_http(state.server_url)
        response = await state.client.post(
            f"{http_url}/api/rooms/{room}/comments",
            json={"author": authors.CLAUDE, "body": body, "quote": quote},
        )
        if response.status_code == 400:
            msg = response.json().get("error", "Bad request")
            _log_tool_result("add_comment", room, t0, msg, error=True)
            return msg
        response.raise_for_status()
        msg = f"Comment added on '{room}': {body!r} anchored to {quote!r}"
        _log_tool_result("add_comment", room, t0, msg)
        return msg
    except httpx.HTTPError as e:
        msg = f"Failed to add comment: {e}"
        _log_tool_result("add_comment", room, t0, msg, error=True)
        return msg
    except Exception:
        _log_tool_error("add_comment", room, t0)
        raise


@mcp.tool()
async def list_comments(room: str, ctx: Context) -> str:
    """List all comments on a document.

    Returns comments from the ASGI server's SQLite store, sorted by creation time.
    Each comment shows: id, author, quoted text, comment body, and resolved status.

    Args:
        room: Document room name
    """
    t0 = _log_tool_entry("list_comments", room)
    try:
        state = _get_state(ctx)
        http_url = _ws_to_http(state.server_url)
        response = await state.client.get(
            f"{http_url}/api/rooms/{room}/comments",
        )
        response.raise_for_status()
        entries = response.json()

        if not entries:
            msg = f"No comments on '{room}'"
            _log_tool_result("list_comments", room, t0, msg)
            return msg

        lines = [f"Comments on '{room}' — {len(entries)} comment(s):\n"]
        for e in entries:
            flags = []
            if e.get("resolved"):
                flags.append("resolved")
            status = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  [{e['id']}] {e['author']}{status}")
            if e.get("quote"):
                lines.append(f"  > {e['quote']}")
            lines.append(f"  {e['body']}")
            lines.append("")

        msg = "\n".join(lines)
        _log_tool_result("list_comments", room, t0, msg)
        return msg
    except httpx.HTTPError:
        msg = "Could not list comments (is the Tafelmusik server running?)"
        _log_tool_result("list_comments", room, t0, msg, error=True)
        return msg
    except Exception:
        _log_tool_error("list_comments", room, t0)
        raise


@mcp.tool()
async def resolve_comment(room: str, quote: str, ctx: Context) -> str:
    """Resolve (dismiss) a comment by its quoted text.

    Finds the comment matching the quote via the ASGI server's HTTP API,
    then resolves it. The browser's yellow underline is removed.

    Args:
        room: Document room name
        quote: The quoted text of the comment to resolve (from list_comments)
    """
    t0 = _log_tool_entry("resolve_comment", room)
    try:
        state = _get_state(ctx)
        http_url = _ws_to_http(state.server_url)

        # List comments to find matching ID(s) by quote text
        response = await state.client.get(
            f"{http_url}/api/rooms/{room}/comments",
        )
        response.raise_for_status()
        entries = response.json()

        matching = [
            e for e in entries
            if not e.get("resolved")
            and e.get("quote", "").strip() == quote.strip()
        ]

        if not matching:
            msg = f"No active comment found with quote: {quote!r}"
            _log_tool_result("resolve_comment", room, t0, msg)
            return msg

        resolved_count = 0
        for entry in matching:
            r = await state.client.post(
                f"{http_url}/api/rooms/{room}/comments/{entry['id']}/resolve",
            )
            if r.status_code == 200:
                resolved_count += 1

        msg = f"Resolved {resolved_count} comment(s) on '{room}' matching {quote!r}"
        _log_tool_result("resolve_comment", room, t0, msg)
        return msg
    except httpx.HTTPError:
        msg = "Could not resolve comment (is the Tafelmusik server running?)"
        _log_tool_result("resolve_comment", room, t0, msg, error=True)
        return msg
    except Exception:
        _log_tool_error("resolve_comment", room, t0)
        raise


if __name__ == "__main__":
    mcp.run()
