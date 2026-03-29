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
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from anyio import Event, create_task_group, sleep
from httpx_ws import aconnect_ws
from mcp.server.fastmcp import Context, FastMCP
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

log = logging.getLogger(__name__)


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


# --- Change observer: debounce + section diff ---


async def _change_consumer(
    conn: RoomConnection,
    room: str,
    debounce: float = 2.0,
) -> None:
    """Consume change events, debounce, and produce section-level diffs.

    Waits for silence (no new changes for ``debounce`` seconds) before
    processing. Accumulates old→new across rapid edits so the diff
    reflects the full change, not each keystroke.
    """
    while True:
        # Wait for first change
        first_old, _ = await conn.change_queue.get()
        latest_new = None

        # Drain additional changes within the debounce window
        while True:
            try:
                _, latest_new_candidate = await asyncio.wait_for(
                    conn.change_queue.get(), timeout=debounce
                )
                latest_new = latest_new_candidate
            except TimeoutError:
                break

        # Use current text if no additional changes arrived
        if latest_new is None:
            latest_new = str(conn.text)

        changes = document.diff_sections(first_old, latest_new)
        if changes:
            log.info("Room %s: remote edit — %s", room, changes)
            conn.pending_notifications.append(changes)


# --- Connection management ---


@dataclass
class RoomConnection:
    """A live connection to a single room on the ASGI server.

    Holds the local Doc (with a Y.Text keyed "content") and events for
    sync status and liveness. The sync loop runs in a background task that
    also owns the WebSocket — they share the same asyncio Task to avoid
    anyio cancel scope cross-task violations.

    The change_queue receives remote edits (origin != "claude") for
    notification to the user. Populated by a synchronous text.observe()
    callback; consumed by an async task that debounces and diffs.
    """

    doc: Doc
    text: Text
    synced: Event
    dead: Event
    _task: asyncio.Task
    change_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_notifications: list = field(default_factory=list)
    _cached_content: str = ""
    _consumer_task: asyncio.Task | None = None
    _observe_subscription: Any = None

    async def close(self) -> None:
        if self._observe_subscription is not None:
            self.text.unobserve(self._observe_subscription)
            self._observe_subscription = None
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
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
    sync_timeout: float = 10.0
    keepalive: float | None = 60.0

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
            synced=synced,
            dead=dead,
            _task=task,
            _cached_content=str(text),
        )

        def _on_text_change(event, txn):
            """Synchronous observe callback — queues remote edits only.

            Always updates the content cache (so diffs are accurate),
            but only queues changes that weren't made by Claude.

            Wrapped in try/except because an unhandled exception here
            would propagate as an ExceptionGroup when the transaction
            commits, crashing the sync loop.
            """
            try:
                old_content = conn._cached_content
                new_content = str(text)
                conn._cached_content = new_content
                if txn.origin != authors.CLAUDE:
                    conn.change_queue.put_nowait((old_content, new_content))
            except Exception:
                log.warning("Observer callback failed for room", exc_info=True)

        conn._observe_subscription = text.observe(_on_text_change)
        conn._consumer_task = asyncio.create_task(_change_consumer(conn, room))

        self.rooms[room] = conn
        return conn

    async def close_all(self) -> None:
        for conn in self.rooms.values():
            await conn.close()
        self.rooms.clear()


# --- FastMCP setup ---


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[AppState]:
    server_url = os.environ.get("TAFELMUSIK_URL", "ws://127.0.0.1:3456")
    async with httpx.AsyncClient() as client:
        state = AppState(client=client, server_url=server_url)
        try:
            yield state
        finally:
            await state.close_all()


mcp = FastMCP("tafelmusik", lifespan=lifespan)


def _get_state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context


# --- Tools ---


@mcp.tool()
async def read_doc(room: str, ctx: Context) -> str:
    """Read the full markdown content of a document.

    Args:
        room: Document room name (e.g. "meeting-notes", "draft")
    """
    state = _get_state(ctx)
    conn = await state.connect(room)
    content = document.read(conn.text)
    if not content:
        return f"(Document '{room}' is empty)"
    return content


@mcp.tool()
async def edit_doc(room: str, content: str, mode: str, ctx: Context) -> str:
    """Edit a document.

    Args:
        room: Document room name
        content: The content to write
        mode: How to apply the edit. One of:
            - "append": Add content to the end
            - "replace_all": Replace the entire document
            - "replace_section": Replace a section by heading (content must start
              with a markdown heading like "## Section Name"). Replaces everything
              from that heading to the next heading of equal or higher level.
              If the heading doesn't exist, appends a new section.
    """
    state = _get_state(ctx)
    conn = await state.connect(room)

    if mode == "append":
        with conn.doc.transaction(origin="claude"):
            conn.text.insert(len(str(conn.text)), content, attrs={"author": "claude"})
        return f"Appended {len(content)} chars to '{room}'"
    elif mode == "replace_all":
        document.replace_all(conn.text, content, author=authors.CLAUDE)
        return f"Replaced all content in '{room}' ({len(content)} chars)"
    elif mode == "replace_section":
        replaced = document.replace_section(conn.text, content, author=authors.CLAUDE)
        heading = content.split("\n", 1)[0].strip()
        verb = "Replaced existing" if replaced else "Appended new"
        return f"{verb} section '{heading}' in '{room}'"
    else:
        return f"Unknown mode '{mode}'. Use 'append', 'replace_all', or 'replace_section'."


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
    document.replace_all(conn.text, markdown, author=authors.CLAUDE)
    return f"Loaded {len(markdown)} chars into '{room}'"


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
        return "Documents:\n" + "\n".join(f"  - {r}" for r in rooms)
    except httpx.HTTPError:
        return "Could not list documents (is the Tafelmusik server running?)"


if __name__ == "__main__":
    mcp.run()
