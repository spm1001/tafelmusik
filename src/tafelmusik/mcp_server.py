"""MCP server entry point — ephemeral, connects to ASGI server as Yjs peer.

Architecture: FastMCP with stdio transport. Maintains persistent WebSocket
connections to the ASGI server — one per room. Each connection runs a pycrdt
Provider that syncs a local Doc with the server's Y.Doc. Tools operate on the
local Doc; the Provider handles bidirectional sync automatically.

Lifecycle: httpx client created at startup (lifespan), room connections
created lazily on first tool call, all cleaned up when the MCP process exits.

Important: aconnect_ws (httpx-ws) uses anyio cancel scopes that are bound to
the asyncio Task that entered them. The Provider must run in the SAME task
that opens the WebSocket. This is why _provider_task() wraps both aconnect_ws
and provider.start() — separating them across tasks causes cancel scope errors.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from anyio import Event
from httpx_ws import aconnect_ws
from mcp.server.fastmcp import Context, FastMCP
from pycrdt import Doc, Text
from pycrdt import (
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    handle_sync_message,
)
from pycrdt.websocket.websocket import HttpxWebsocket
from pycrdt.websocket.yroom import Provider

from tafelmusik import document

log = logging.getLogger(__name__)


def _ws_to_http(url: str) -> str:
    """Convert ws:// or wss:// URL to http:// or https:// for httpx."""
    return url.replace("ws://", "http://").replace("wss://", "https://")


# --- Sync-aware Provider ---
#
# SyncAwareProvider overrides Provider._run() and calls Provider._send_updates().
# Both are private APIs — Provider's public surface is just start() and stop().
# We also access _doc, _channel, and _task_group (set by Provider.__init__).
# Validated against pycrdt 0.12.50. If upgrading pycrdt, re-run tests.

_PROVIDER_PRIVATE_ATTRS = ("_doc", "_channel", "_task_group", "_send_updates", "_run")
for _attr in _PROVIDER_PRIVATE_ATTRS:
    if not hasattr(Provider, _attr) and _attr in ("_send_updates", "_run"):
        raise ImportError(
            f"Provider.{_attr} not found — pycrdt API may have changed. "
            f"SyncAwareProvider was validated against pycrdt 0.12.50."
        )


class SyncAwareProvider(Provider):
    """Provider subclass that exposes a deterministic 'synced' event.

    The base Provider's `started` event fires before SYNC_STEP1 is even sent.
    This subclass intercepts the SYNC_STEP2 response — the message that delivers
    the server's full state — and sets `synced` immediately after applying it.
    """

    def __init__(self, *args, synced: Event | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.synced = synced or Event()

    async def _run(self):
        sync_message = create_sync_message(self._doc)
        await self._channel.send(sync_message)
        assert self._task_group is not None
        self._task_group.start_soon(self._send_updates)
        async for message in self._channel:
            if message[0] == YMessageType.SYNC:
                msg_type = YSyncMessageType(message[1])
                reply = handle_sync_message(message[1:], self._doc)
                if reply is not None:
                    await self._channel.send(reply)
                if msg_type == YSyncMessageType.SYNC_STEP2 and not self.synced.is_set():
                    self.synced.set()
        # Channel iterator ended — connection lost. Cancel the task group
        # so _send_updates stops and the provider task completes. Without
        # this, _send_updates hangs forever waiting for local events,
        # provider.start() never returns, and the dead event never fires.
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()


# --- Connection management ---


@dataclass
class RoomConnection:
    """A live connection to a single room on the ASGI server.

    Holds the local Doc (with a Y.Text keyed "content") and events for
    sync status and liveness. The Provider runs in a background task that
    also owns the WebSocket — they share the same asyncio Task to avoid
    anyio cancel scope cross-task violations.
    """

    doc: Doc
    text: Text
    synced: Event
    dead: Event
    _task: asyncio.Task

    async def close(self) -> None:
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

        async def _provider_task():
            """Run WebSocket + Provider in the same asyncio Task.

            aconnect_ws cancel scopes are task-bound — entering the WebSocket
            in one task and running the Provider in another causes RuntimeError.
            This function keeps both in the same task.
            """
            try:
                async with aconnect_ws(f"{http_url}/{room}", self.client) as ws:
                    channel = HttpxWebsocket(ws, room)
                    provider = SyncAwareProvider(doc, channel, synced=synced)
                    await provider.start()
            except Exception:
                log.warning("Provider task for room %s failed", room, exc_info=True)
            finally:
                dead.set()

        task = asyncio.create_task(_provider_task())

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
            doc=doc, text=text, synced=synced, dead=dead, _task=task,
        )
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
        conn.text += content
        return f"Appended {len(content)} chars to '{room}'"
    elif mode == "replace_all":
        document.replace_all(conn.text, content)
        return f"Replaced all content in '{room}' ({len(content)} chars)"
    elif mode == "replace_section":
        replaced = document.replace_section(conn.text, content)
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
    document.replace_all(conn.text, markdown)
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
