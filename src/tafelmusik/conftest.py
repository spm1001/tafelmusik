"""Shared test fixtures and helpers."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from anyio import Event
from httpx_ws import aconnect_ws
from pycrdt import (
    Doc,
    Text,
    YMessageType,
    handle_sync_message,
)

from tafelmusik.mcp_server import WebSocketChannel, _sync_loop


def get_free_port() -> int:
    """Find a free TCP port by briefly binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Mock channel for unit-testing _sync_loop ---


class MockChannel:
    """In-memory channel that simulates a Yjs peer.

    When ``peer_doc`` is provided, automatically responds to SYNC_STEP1
    with SYNC_STEP2 (like a real server would).  Messages can also be
    injected manually via ``inject()``.
    """

    def __init__(self, peer_doc: Doc | None = None) -> None:
        self.sent: list[bytes] = []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._peer_doc = peer_doc

    async def send(self, message: bytes) -> None:
        self.sent.append(message)
        # Auto-respond to sync messages when a peer doc is configured
        if self._peer_doc is not None and message[0] == YMessageType.SYNC:
            reply = handle_sync_message(message[1:], self._peer_doc)
            if reply is not None:
                self._queue.put_nowait(reply)

    def inject(self, message: bytes) -> None:
        """Inject a message as if received from the peer."""
        self._queue.put_nowait(message)

    def close(self) -> None:
        """Signal end of channel iteration."""
        self._queue.put_nowait(None)

    def __aiter__(self) -> MockChannel:
        return self

    async def __anext__(self) -> bytes:
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration()
        return msg


# --- Test helper: connect as a Yjs peer ---


@asynccontextmanager
async def connect_peer(port: int, room: str) -> AsyncIterator[Text]:
    """Connect a Yjs peer to the server using the standalone sync protocol.

    Uses deterministic sync detection (SYNC_STEP2 event) instead of
    sleep-based heuristics.  The WebSocket and sync loop share the same
    asyncio Task to satisfy anyio cancel scope constraints.
    """
    doc = Doc()
    doc["content"] = text = Text()
    synced = Event()

    async def _task() -> None:
        async with httpx.AsyncClient() as client:
            async with aconnect_ws(f"http://127.0.0.1:{port}/_ws/{room}", client) as ws:
                channel = WebSocketChannel(ws)
                await _sync_loop(doc, channel, synced, keepalive=None)

    task = asyncio.create_task(_task())
    try:
        await asyncio.wait_for(synced.wait(), timeout=5.0)
        yield text
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
