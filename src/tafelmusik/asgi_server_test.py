"""Tests for ASGI server."""

import asyncio

import httpx
import pytest
import uvicorn
from httpx_ws import aconnect_ws
from pycrdt import Doc, Text
from pycrdt.websocket.websocket import HttpxWebsocket
from pycrdt.websocket.yroom import Provider


@pytest.fixture
async def server(tmp_path, monkeypatch):
    """Start a tafelmusik server on an ephemeral port with a temp DB."""
    monkeypatch.setattr(
        "tafelmusik.asgi_server.TafelmusikStore.db_path",
        str(tmp_path / "test.db"),
    )
    # Reimport to pick up monkeypatched db_path
    import importlib

    import tafelmusik.asgi_server

    importlib.reload(tafelmusik.asgi_server)
    app = tafelmusik.asgi_server.app

    port = 13470
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)
    yield port
    srv.should_exit = True
    await asyncio.sleep(0.5)
    task.cancel()


async def _connect_client(port: int, room: str) -> tuple[Text, Provider]:
    """Connect a pycrdt client to the server and return (text, provider)."""
    doc = Doc()
    doc["content"] = text = Text()
    client = httpx.AsyncClient()
    ws = await client.__aenter__()  # noqa — managed by caller
    return text, None  # placeholder


async def _write_and_read(port: int, room: str, content: str) -> str:
    """Write content to a room via Yjs client, return what a second client reads."""
    doc1 = Doc()
    doc1["content"] = t1 = Text()

    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/{room}", client) as ws:
            ch = HttpxWebsocket(ws, room)
            p = Provider(doc1, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            t1 += content
            await asyncio.sleep(0.5)
            await p.stop()

    # Second client reads
    doc2 = Doc()
    doc2["content"] = t2 = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/{room}", client) as ws:
            ch = HttpxWebsocket(ws, room)
            p = Provider(doc2, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            result = str(t2)
            await p.stop()

    return result


async def test_two_clients_sync(server):
    """Two clients editing the same room see each other's changes."""
    port = server
    doc1 = Doc()
    doc1["content"] = t1 = Text()
    doc2 = Doc()
    doc2["content"] = t2 = Text()

    async with httpx.AsyncClient() as c1, httpx.AsyncClient() as c2:
        async with aconnect_ws(f"http://127.0.0.1:{port}/sync-test", c1) as ws1:
            ch1 = HttpxWebsocket(ws1, "sync-test")
            p1 = Provider(doc1, ch1)
            asyncio.create_task(p1.start())
            await asyncio.sleep(0.5)

            async with aconnect_ws(f"http://127.0.0.1:{port}/sync-test", c2) as ws2:
                ch2 = HttpxWebsocket(ws2, "sync-test")
                p2 = Provider(doc2, ch2)
                asyncio.create_task(p2.start())
                await asyncio.sleep(0.5)

                t1 += "Hello from client 1"
                await asyncio.sleep(0.5)
                assert "Hello from client 1" in str(t2)

                t2 += "\nHello from client 2"
                await asyncio.sleep(0.5)
                assert "Hello from client 2" in str(t1)

                await p2.stop()
            await p1.stop()


async def test_multi_room(server):
    """Different rooms are independent."""
    port = server

    await _write_and_read(port, "room-a", "Content A")
    await _write_and_read(port, "room-b", "Content B")

    # Verify isolation
    doc = Doc()
    doc["content"] = text = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/room-a", client) as ws:
            ch = HttpxWebsocket(ws, "room-a")
            p = Provider(doc, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            content = str(text)
            assert "Content A" in content
            assert "Content B" not in content
            await p.stop()
