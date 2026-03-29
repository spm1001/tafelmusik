"""Tests for ASGI server."""

import asyncio

import httpx
import pytest
import uvicorn
from httpx_ws import aconnect_ws
from pycrdt import Doc, Text
from pycrdt.websocket.websocket import HttpxWebsocket
from pycrdt.websocket.yroom import Provider

from tafelmusik.asgi_server import create_app
from tafelmusik.conftest import get_free_port


@pytest.fixture
async def server(tmp_path):
    """Start a tafelmusik server with a temp DB on a free port."""
    app = create_app(db_path=tmp_path / "test.db", public_dir=tmp_path)
    # Write a minimal index.html so static files mount works
    (tmp_path / "index.html").write_text("<html></html>")

    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)
    yield port
    srv.should_exit = True
    await asyncio.sleep(0.5)
    task.cancel()


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

    # Write to room-a
    doc_a = Doc()
    doc_a["content"] = ta = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/room-a", client) as ws:
            ch = HttpxWebsocket(ws, "room-a")
            p = Provider(doc_a, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            ta += "Content A"
            await asyncio.sleep(0.5)
            await p.stop()

    # Write to room-b
    doc_b = Doc()
    doc_b["content"] = tb = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/room-b", client) as ws:
            ch = HttpxWebsocket(ws, "room-b")
            p = Provider(doc_b, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            tb += "Content B"
            await asyncio.sleep(0.5)
            await p.stop()

    # Read room-a — should not contain room-b's content
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


async def test_http_serves_index(server):
    """HTTP GET / serves index.html."""
    port = server
    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://127.0.0.1:{port}/")
        assert r.status_code == 200
        assert "<html>" in r.text


async def test_server_survives_send_after_disconnect(server):
    """Server handles send to a disconnected client without crashing.

    Starlette's WebSocket state machine raises WebSocketDisconnect on the
    first failed send, then RuntimeError on subsequent sends. Both must be
    caught in StarletteWebsocket.send() to prevent crashing the entire
    WebsocketServer via pycrdt's task group.
    """
    port = server

    # Client 1: connect, write content, then disconnect abruptly
    doc1 = Doc()
    doc1["content"] = t1 = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/send-crash-test", client) as ws:
            ch = HttpxWebsocket(ws, "send-crash-test")
            p = Provider(doc1, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            t1 += "Content from client 1"
            await asyncio.sleep(0.5)
            await p.stop()
    # WebSocket closed — server may still try to send to this dead channel

    await asyncio.sleep(0.5)

    # Client 2: connect to the SAME room — proves server is still alive
    doc2 = Doc()
    doc2["content"] = t2 = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/send-crash-test", client) as ws:
            ch = HttpxWebsocket(ws, "send-crash-test")
            p = Provider(doc2, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            assert "Content from client 1" in str(t2)
            await p.stop()

    # Client 3: connect to a DIFFERENT room — proves entire server is healthy
    doc3 = Doc()
    doc3["content"] = t3 = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/unrelated-room", client) as ws:
            ch = HttpxWebsocket(ws, "unrelated-room")
            p = Provider(doc3, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            t3 += "Still alive"
            await asyncio.sleep(0.3)
            assert "Still alive" in str(t3)
            await p.stop()


async def test_persistence_across_restart(tmp_path):
    """Content written to one server instance is restored by a fresh instance."""
    db_path = tmp_path / "persist.db"
    (tmp_path / "index.html").write_text("<html></html>")
    port = get_free_port()

    # Server 1: write content
    app1 = create_app(db_path=db_path, public_dir=tmp_path)
    config1 = uvicorn.Config(app1, host="127.0.0.1", port=port, log_level="error")
    srv1 = uvicorn.Server(config1)
    task1 = asyncio.create_task(srv1.serve())
    await asyncio.sleep(1)

    doc1 = Doc()
    doc1["content"] = t1 = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/persist-test", client) as ws:
            ch = HttpxWebsocket(ws, "persist-test")
            p = Provider(doc1, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(0.5)
            t1 += "# Persistence\n\nSurvives restart."
            await asyncio.sleep(0.5)
            await p.stop()

    srv1.should_exit = True
    await asyncio.sleep(0.5)
    task1.cancel()

    # Server 2: fresh app, same db_path — content should be restored
    app2 = create_app(db_path=db_path, public_dir=tmp_path)
    config2 = uvicorn.Config(app2, host="127.0.0.1", port=port, log_level="error")
    srv2 = uvicorn.Server(config2)
    task2 = asyncio.create_task(srv2.serve())
    await asyncio.sleep(1)

    doc2 = Doc()
    doc2["content"] = t2 = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{port}/persist-test", client) as ws:
            ch = HttpxWebsocket(ws, "persist-test")
            p = Provider(doc2, ch)
            asyncio.create_task(p.start())
            await asyncio.sleep(1)
            result = str(t2)
            assert "Survives restart." in result, f"Expected persisted content, got: {result!r}"
            await p.stop()

    srv2.should_exit = True
    await asyncio.sleep(0.5)
    task2.cancel()
