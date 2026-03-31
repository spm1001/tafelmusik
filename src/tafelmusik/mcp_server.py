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
import logging
import os
import subprocess
import time
from collections.abc import AsyncIterator
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
    Map,
    Text,
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)

from tafelmusik import authors, comments, document

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
                        "drift": drift,
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

    Holds the local Doc (with a Y.Text keyed "content" and Y.Map keyed
    "comments") and events for sync status and liveness. The sync loop
    runs in a background task that also owns the WebSocket — they share
    the same asyncio Task to avoid anyio cancel scope cross-task violations.

    Two observers:
    - Text observer: resets idle timer on every remote change. Does NOT
      send notifications directly.
    - Comment observer: detects new comments from non-Claude authors,
      queues them for notification via _comment_queue.

    Idle timer: fires after IDLE_TIMEOUT seconds of no remote edits.
    If drift is high, pushes a full resync.
    """

    doc: Doc
    text: Text
    comments_map: Map
    synced: Event
    dead: Event
    _task: asyncio.Task
    _observe_subscription: Any = None  # text observer (idle timer)
    _comment_observe_subscription: Any = None  # comment observer
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
        if self._comment_observe_subscription is not None:
            self.comments_map.unobserve(self._comment_observe_subscription)
            self._comment_observe_subscription = None
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
                return conn
            # Connection died — clean up before reconnecting
            log.info("Room %s connection lost, reconnecting", room)
            await conn.close()
            del self.rooms[room]

        # httpx-ws uses http:// for the WebSocket upgrade
        http_url = _ws_to_http(self.server_url)

        doc = Doc()
        doc["content"] = text = Text()
        comments_map: Map = doc.get("comments", type=Map)
        synced = Event()
        dead = Event()

        async def _sync_task():
            """Run WebSocket + sync protocol in the same asyncio Task.

            aconnect_ws cancel scopes are task-bound — entering the WebSocket
            in one task and running the sync loop in another causes RuntimeError.
            This function keeps both in the same task.
            """
            try:
                async with aconnect_ws(f"{http_url}/{room}", self.client) as ws:
                    channel = WebSocketChannel(ws)
                    await _sync_loop(doc, channel, synced, keepalive=self.keepalive)
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
            # Check dead BEFORE cancelling — cancellation always sets dead
            # via the finally block, so checking after would always be True.
            failed_fast = dead.is_set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            if failed_fast:
                raise ConnectionError(
                    f"Connection to room '{room}' failed. "
                    f"Is the Tafelmusik server running on {self.server_url}?"
                )
            raise TimeoutError(
                f"Sync with Tafelmusik server timed out after {self.sync_timeout}s "
                f"for room '{room}'. Is the server running and responding?"
            )

        conn = RoomConnection(
            doc=doc,
            text=text,
            comments_map=comments_map,
            synced=synced,
            dead=dead,
            _task=task,
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
                    loop = asyncio.get_event_loop()
                    conn._idle_timer = loop.call_later(
                        self.idle_timeout,
                        lambda: asyncio.ensure_future(_push_resync(conn, room)),
                    )
            except Exception:
                log.warning("Text observer callback failed for room", exc_info=True)

        def _on_comments_change(event, txn):
            """Synchronous observe callback — queues new comments for notification.

            Fires when the Y.Map 'comments' changes. Only queues new comments
            (action == "add") from non-Claude authors. Edits to existing
            comments (resolved, re-anchored) are ignored.

            Wrapped in try/except because an unhandled exception here
            would propagate as an ExceptionGroup when the transaction
            commits, crashing the sync loop.
            """
            try:
                if txn.origin == authors.CLAUDE:
                    return
                for key, change in event.keys.items():
                    if change["action"] != "add":
                        continue
                    new_val = change["newValue"]
                    if not isinstance(new_val, Map):
                        continue
                    conn._comment_queue.put_nowait(
                        {
                            "comment_id": key,
                            "author": new_val.get("author", "unknown"),
                            "quote": new_val.get("quote", ""),
                            "body": new_val.get("body", ""),
                        }
                    )
            except Exception:
                log.warning("Comment observer callback failed for room", exc_info=True)

        conn._observe_subscription = text.observe(_on_text_change)
        conn._comment_observe_subscription = comments_map.observe(_on_comments_change)
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


# --- FastMCP setup ---


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[AppState]:
    server_url = os.environ.get("TAFELMUSIK_URL", "ws://127.0.0.1:3456")
    async with httpx.AsyncClient() as client:
        state = AppState(client=client, server_url=server_url)
        await state.start_room_poller()
        try:
            yield state
        finally:
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


def _reanchor_summary(result: dict) -> str:
    """Format re-anchoring results as a suffix for edit responses."""
    parts = []
    if result["reanchored"]:
        parts.append(f"{len(result['reanchored'])} comment(s) re-anchored")
    if result["orphaned"]:
        parts.append(f"{len(result['orphaned'])} comment(s) orphaned")
    return f" — {', '.join(parts)}" if parts else ""


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
    state = _get_state(ctx)
    conn = await state.connect(room)

    if mode in ("append", "replace_section") and not content:
        return f"{mode} mode requires 'content' parameter"

    if mode == "append":
        with conn.doc.transaction(origin=authors.CLAUDE):
            conn.text.insert(len(str(conn.text)), content, attrs={"author": authors.CLAUDE})
        return f"Appended {len(content)} chars to '{room}'"
    elif mode == "replace_all":
        comments_map: Map = conn.doc.get("comments", type=Map)
        doc_len = len(str(conn.text))
        affected = comments.collect_affected(conn.text, comments_map, 0, doc_len)
        document.replace_all(conn.text, content, author=authors.CLAUDE)
        result = comments.reanchor(conn.text, comments_map, affected, author=authors.CLAUDE)
        suffix = _reanchor_summary(result)
        return f"Replaced all content in '{room}' ({len(content)} chars){suffix}"
    elif mode == "replace_section":
        try:
            comments_map: Map = conn.doc.get("comments", type=Map)
            doc_content = str(conn.text)
            heading = content.split("\n", 1)[0].strip()
            bounds = document.find_section(doc_content, heading)
            if bounds:
                sec_start, sec_end = bounds
                affected = comments.collect_affected(conn.text, comments_map, sec_start, sec_end)
            else:
                affected = []  # new section — nothing to re-anchor
            replaced = document.replace_section(conn.text, content, author=authors.CLAUDE)
            if affected:
                # Search within the new section's bounds
                new_bounds = document.find_section(str(conn.text), heading)
                if new_bounds:
                    result = comments.reanchor(
                        conn.text,
                        comments_map,
                        affected,
                        search_start=new_bounds[0],
                        search_end=new_bounds[1],
                        author=authors.CLAUDE,
                    )
                else:
                    result = comments.reanchor(
                        conn.text, comments_map, affected, author=authors.CLAUDE
                    )
            else:
                result = {"reanchored": [], "orphaned": []}
        except ValueError as e:
            return str(e)
        verb = "Replaced existing" if replaced else "Appended new"
        suffix = _reanchor_summary(result)
        return f"{verb} section '{heading}' in '{room}'{suffix}"
    elif mode == "patch":
        if not find:
            return "patch mode requires 'find' parameter"
        try:
            comments_map: Map = conn.doc.get("comments", type=Map)
            doc_content = str(conn.text)
            patch_start = doc_content.find(find)
            if patch_start != -1:
                patch_end = patch_start + len(find)
                affected = comments.collect_affected(
                    conn.text, comments_map, patch_start, patch_end
                )
            else:
                affected = []
            document.patch(conn.text, find, replace, author=authors.CLAUDE)
            if affected:
                # Search region: where the patch landed + replacement length
                search_end = patch_start + len(replace) if replace else patch_start
                result = comments.reanchor(
                    conn.text,
                    comments_map,
                    affected,
                    search_start=patch_start,
                    search_end=max(search_end, patch_start + 1),
                    author=authors.CLAUDE,
                )
            else:
                result = {"reanchored": [], "orphaned": []}
        except ValueError as e:
            return f"Patch failed: {e}"
        action = "Deleted" if not replace else "Patched"
        suffix = _reanchor_summary(result)
        return f"{action} {len(find)} chars in '{room}'{suffix}"
    else:
        return f"Unknown mode '{mode}'. Use 'append', 'replace_all', 'replace_section', or 'patch'."


@mcp.tool()
async def load_doc(room: str, markdown: str, ctx: Context) -> str:
    """Load markdown into a document, replacing any existing content.

    This is the simplest way to populate a document — it clears everything
    and writes the given markdown.

    Args:
        room: Document room name
        markdown: The full markdown content to load
    """
    state = _get_state(ctx)
    conn = await state.connect(room)
    comments_map: Map = conn.doc.get("comments", type=Map)
    doc_len = len(str(conn.text))
    affected = comments.collect_affected(conn.text, comments_map, 0, doc_len)
    document.replace_all(conn.text, markdown, author=authors.CLAUDE)
    result = comments.reanchor(conn.text, comments_map, affected, author=authors.CLAUDE)
    suffix = _reanchor_summary(result)
    return f"Loaded {len(markdown)} chars into '{room}'{suffix}"


@mcp.tool()
async def list_docs(ctx: Context) -> str:
    """List documents available on the server.

    Returns room names from both active connections and persisted storage.
    """
    state = _get_state(ctx)
    http_url = _ws_to_http(state.server_url)
    try:
        response = await state.client.get(f"{http_url}/api/rooms")
        response.raise_for_status()
        data = response.json()
        rooms = data.get("rooms", [])
        if not rooms:
            return "No documents found."
        lines = []
        for entry in rooms:
            if isinstance(entry, str):
                lines.append(f"  - {entry}")
            else:
                name = entry["name"]
                marker = " (active)" if entry.get("active") else ""
                lines.append(f"  - {name}{marker}")
        return "Documents:\n" + "\n".join(lines)
    except httpx.HTTPError:
        return "Could not list documents (is the Tafelmusik server running?)"


@mcp.tool()
async def flush_doc(room: str, ctx: Context) -> str:
    """Flush document to .md file on disk, wipe comments, git commit.

    Writes current Y.Text content to the .md file, clears all comments
    from the Y.Map, and commits the file to git. This is the "save" —
    the .md file becomes the durable artifact.

    Args:
        room: Document room name (maps to file path relative to docs_dir)
    """
    state = _get_state(ctx)
    conn = await state.connect(room)

    content = str(conn.text)
    docs_dir = _get_docs_dir(state)
    md_path = (docs_dir / f"{room}.md").resolve()
    if not md_path.is_relative_to(docs_dir.resolve()):
        return f"Room name '{room}' escapes docs directory — rejected."
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content)

    # Wipe comments
    comments_map: Map = conn.doc.get("comments", type=Map)
    wiped = comments.clear_all(conn.doc, comments_map, author=authors.CLAUDE)

    # Git commit the file (only if docs_dir is inside a git repo)
    git_status = ""
    repo_root = _find_git_root(docs_dir)
    if repo_root:
        git_msg = f"flush: {room}"
        try:
            subprocess.run(
                ["git", "add", str(md_path)],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", git_msg, "--", str(md_path)],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
            )
            git_status = " Git committed."
        except subprocess.CalledProcessError:
            git_status = " Git commit skipped (no changes)."
    else:
        git_status = " No git repo found — skipped commit."

    comment_note = f" {wiped} comment(s) cleared." if wiped else ""
    return f"Flushed {len(content)} chars to {md_path}.{comment_note}{git_status}"


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
    state = _get_state(ctx)
    conn = await state.connect(room)

    drift = _compute_drift(conn, room)
    drift_status = "stale" if drift > DRIFT_THRESHOLD else "fresh"
    header = f"Document '{room}' — drift: {drift}B ({drift_status}, threshold: {DRIFT_THRESHOLD}B)"

    chunks = conn.text.diff()
    if not chunks:
        return f"{header}\n(empty)"
    lines = []
    for content, attrs in chunks:
        preview = repr(content) if len(content) <= 80 else repr(content[:77] + "...")
        if attrs:
            lines.append(f"  {preview}  attrs={attrs}")
        else:
            lines.append(f"  {preview}")
    return f"{header}\n{len(chunks)} chunk(s):\n" + "\n".join(lines)


@mcp.tool()
async def add_comment(room: str, quote: str, body: str, ctx: Context) -> str:
    """Add a comment anchored to specific text in a document.

    Finds the quote text in the document, creates anchors at that position,
    and stores the comment in the Y.Map 'comments'. The comment appears in
    the browser's comments pane with author='claude'.

    Args:
        room: Document room name
        quote: Exact text to comment on (must exist in the document)
        body: The comment text
    """
    state = _get_state(ctx)
    conn = await state.connect(room)
    comments_map: Map = conn.doc.get("comments", type=Map)
    try:
        comments.add_comment(
            conn.doc, conn.text, comments_map, quote, body, author=authors.CLAUDE
        )
    except ValueError as e:
        return str(e)
    return f"Comment added on '{room}': {body!r} anchored to {quote!r}"


@mcp.tool()
async def list_comments(room: str, ctx: Context) -> str:
    """List all comments on a document.

    Returns comments from the Y.Map 'comments', sorted by document position.
    Each comment shows: author, quoted text, comment body, and resolved status.

    Args:
        room: Document room name
    """
    state = _get_state(ctx)
    conn = await state.connect(room)
    comments_map: Map = conn.doc.get("comments", type=Map)
    entries = comments.list_comments(comments_map)

    if not entries:
        return f"No comments on '{room}'"

    lines = [f"Comments on '{room}' — {len(entries)} comment(s):\n"]
    for e in entries:
        flags = []
        if e["resolved"]:
            flags.append("resolved")
        if e["orphaned"]:
            flags.append("orphaned")
        status = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {e['author']}{status}")
        lines.append(f"  > {e['quote']}")
        lines.append(f"  {e['body']}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def resolve_comment(room: str, quote: str, ctx: Context) -> str:
    """Resolve (dismiss) a comment by its quoted text.

    Marks the comment as resolved. It disappears from the browser's
    comments pane and its yellow underline is removed.

    Args:
        room: Document room name
        quote: The quoted text of the comment to resolve (from list_comments)
    """
    state = _get_state(ctx)
    conn = await state.connect(room)
    comments_map: Map = conn.doc.get("comments", type=Map)
    resolved_count = comments.resolve_comment(
        conn.doc, comments_map, quote, author=authors.CLAUDE
    )
    if resolved_count == 0:
        return f"No active comment found with quote: {quote!r}"
    return f"Resolved {resolved_count} comment(s) on '{room}' matching {quote!r}"


if __name__ == "__main__":
    mcp.run()
