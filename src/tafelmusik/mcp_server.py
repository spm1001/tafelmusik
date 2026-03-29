"""MCP server entry point — ephemeral, connects to ASGI server as Yjs peer.

Architecture: FastMCP with stdio transport. Maintains persistent WebSocket
connections to the ASGI server — one per room. Each connection runs a pycrdt
Provider that syncs a local Doc with the server's Y.Doc. Tools operate on the
local Doc; the Provider handles bidirectional sync automatically.

Lifecycle: httpx client created at startup (lifespan), room connections
created lazily on first tool call, all cleaned up when the MCP process exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import httpx
from anyio import Event
from httpx_ws import aconnect_ws
from mcp.server.fastmcp import Context, FastMCP
from pycrdt import Doc, Text
from pycrdt._sync import (
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    handle_sync_message,
)
from pycrdt.websocket.websocket import HttpxWebsocket
from pycrdt.websocket.yroom import Provider

from tafelmusik import document

log = logging.getLogger(__name__)


# --- Sync-aware Provider ---


class SyncAwareProvider(Provider):
    """Provider subclass that exposes a deterministic 'synced' event.

    The base Provider's `started` event fires before SYNC_STEP1 is even sent.
    This subclass intercepts the SYNC_STEP2 response — the message that delivers
    the server's full state — and sets `synced` immediately after applying it.

    Usage:
        provider = SyncAwareProvider(doc, channel)
        task = asyncio.create_task(provider.start())
        await provider.synced.wait()  # doc now has full server state
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.synced = Event()

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
        # this, _send_updates hangs forever waiting for local events, and
        # the dead connection is never detected by _task.done().
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()


# --- Connection management ---


@dataclass
class RoomConnection:
    """A live connection to a single room on the ASGI server.

    Holds the local Doc (with a Y.Text keyed "content"), a Provider
    that syncs it over WebSocket, and the AsyncExitStack that owns
    the WebSocket's lifetime.
    """

    doc: Doc
    text: Text
    provider: Provider
    _task: asyncio.Task
    _exit_stack: AsyncExitStack

    async def close(self) -> None:
        # Stop the Provider first (cancels its internal task group)
        try:
            await self.provider.stop()
        except Exception:
            pass  # Provider may already be stopped if connection was lost
        # Wait for the Provider task to fully complete before closing the WebSocket
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await self._exit_stack.aclose()
        except RuntimeError:
            pass  # Dead connection — aconnect_ws cleanup may fail on cancel scopes


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

    async def connect(self, room: str) -> RoomConnection:
        """Get or create a connection to a room.

        If a previous connection exists but died (server restart, network),
        it is cleaned up and a fresh one is created.
        """
        if room in self.rooms:
            conn = self.rooms[room]
            if not conn._task.done():
                return conn
            # Connection died — clean up before reconnecting
            log.info("Room %s connection lost, reconnecting", room)
            await conn.close()
            del self.rooms[room]

        # httpx-ws uses http:// for the WebSocket upgrade
        http_url = self.server_url.replace("ws://", "http://").replace("wss://", "https://")

        stack = AsyncExitStack()
        ws = await stack.enter_async_context(
            aconnect_ws(f"{http_url}/{room}", self.client)
        )

        channel = HttpxWebsocket(ws, room)
        doc = Doc()
        doc["content"] = text = Text()
        provider = SyncAwareProvider(doc, channel)
        task = asyncio.create_task(provider.start())
        await provider.synced.wait()  # deterministic — fires after SYNC_STEP2 applied

        conn = RoomConnection(
            doc=doc, text=text, provider=provider,
            _task=task, _exit_stack=stack,
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
    http_url = state.server_url.replace("ws://", "http://").replace("wss://", "https://")
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
