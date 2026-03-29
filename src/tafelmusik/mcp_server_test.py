"""Tests for MCP server — integration tests against a real ASGI server.

Tests the AppState/RoomConnection connection management and tool logic
by connecting to a live server instance.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from httpx_ws import aconnect_ws
from pycrdt import Doc, Text
from pycrdt.websocket.websocket import HttpxWebsocket
from pycrdt.websocket.yroom import Provider

from tafelmusik.asgi_server import create_app
from tafelmusik.conftest import get_free_port
from tafelmusik.mcp_server import AppState


@pytest.fixture
async def server(tmp_path):
    """Start a tafelmusik server with a temp DB on a free port."""
    app = create_app(db_path=tmp_path / "test.db", public_dir=tmp_path)
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


@asynccontextmanager
async def make_state(port):
    """Create an AppState with setup and teardown in the same task.

    Using a context manager (not a fixture) ensures that the anyio cancel
    scopes inside aconnect_ws are entered and exited from the same asyncio
    Task. Pytest-asyncio fixture teardown can run in a different task context.
    """
    async with httpx.AsyncClient() as client:
        state = AppState(client=client, server_url=f"ws://127.0.0.1:{port}")
        try:
            yield state
        finally:
            await state.close_all()


async def test_connect_and_read_empty(server):
    """Connecting to a new room gives empty text."""
    async with make_state(server) as state:
        conn = await state.connect("test-empty")
        assert str(conn.text) == ""


async def test_write_then_read(server):
    """Content written via MCP is readable via MCP."""
    async with make_state(server) as state:
        conn = await state.connect("test-write-read")
        conn.text += "# Hello\n\nWorld\n"
        assert "Hello" in str(conn.text)
        assert "World" in str(conn.text)


async def test_mcp_write_visible_to_browser_client(server):
    """Content written via MCP appears in a separate pycrdt client (simulates browser)."""
    async with make_state(server) as state:
        # Write via MCP
        conn = await state.connect("test-mcp-to-browser")
        conn.text += "# From Claude\n\nThis was written by the MCP server.\n"
        await asyncio.sleep(0.5)

        # Read via separate pycrdt client (simulates browser)
        doc = Doc()
        doc["content"] = text = Text()
        async with httpx.AsyncClient() as client:
            async with aconnect_ws(f"http://127.0.0.1:{server}/test-mcp-to-browser", client) as ws:
                channel = HttpxWebsocket(ws, "test-mcp-to-browser")
                provider = Provider(doc, channel)
                asyncio.create_task(provider.start())
                await asyncio.sleep(0.5)

                assert "From Claude" in str(text)
                assert "written by the MCP server" in str(text)
                await provider.stop()


async def test_browser_write_visible_to_mcp(server):
    """Content written by a separate pycrdt client is readable via MCP."""
    # Write via separate client (simulates browser)
    doc = Doc()
    doc["content"] = text = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{server}/test-browser-to-mcp", client) as ws:
            channel = HttpxWebsocket(ws, "test-browser-to-mcp")
            provider = Provider(doc, channel)
            asyncio.create_task(provider.start())
            await asyncio.sleep(0.5)

            text += "# From Browser\n\nSameer typed this.\n"
            await asyncio.sleep(0.5)
            await provider.stop()

    # Read via MCP
    async with make_state(server) as state:
        conn = await state.connect("test-browser-to-mcp")
        content = str(conn.text)
        assert "From Browser" in content
        assert "Sameer typed this" in content


async def test_replace_section_via_mcp(server):
    """replace_section works through the MCP connection layer."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("test-replace-section")
        conn.text += "# Doc\n\nIntro\n\n## API\n\nOld API text\n\n## Usage\n\nUsage text\n"
        await asyncio.sleep(0.3)

        document.replace_section(conn.text, "## API\n\nNew API documentation\n")
        content = str(conn.text)
        assert "New API documentation" in content
        assert "Old API text" not in content
        assert "## Usage\n\nUsage text" in content


async def test_replace_all_via_mcp(server):
    """replace_all clears and rewrites the document."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("test-replace-all")
        conn.text += "Old content that should be gone"
        await asyncio.sleep(0.3)

        document.replace_all(conn.text, "# Fresh Start\n\nCompletely new.\n")
        content = str(conn.text)
        assert "Fresh Start" in content
        assert "Old content" not in content


async def test_reconnect_same_room(server):
    """Connecting to the same room twice returns the same connection."""
    async with make_state(server) as state:
        conn1 = await state.connect("test-reconnect")
        conn2 = await state.connect("test-reconnect")
        assert conn1 is conn2


async def test_list_rooms_endpoint(server):
    """The /api/rooms endpoint returns room names."""
    doc = Doc()
    doc["content"] = text = Text()
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{server}/listed-room", client) as ws:
            channel = HttpxWebsocket(ws, "listed-room")
            provider = Provider(doc, channel)
            asyncio.create_task(provider.start())
            await asyncio.sleep(0.5)

            text += "Some content"
            await asyncio.sleep(0.5)

            response = await client.get(f"http://127.0.0.1:{server}/api/rooms")
            assert response.status_code == 200
            data = response.json()
            assert "listed-room" in data["rooms"]

            await provider.stop()


async def test_sync_timeout(tmp_path):
    """AppState.connect raises TimeoutError if server never sends SYNC_STEP2."""
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket as StarletteWS

    async def silent_ws(websocket: StarletteWS):
        await websocket.accept()
        # Accept connection but never respond to sync protocol
        try:
            while True:
                await websocket.receive_bytes()
        except Exception:
            pass

    app = Starlette(routes=[WebSocketRoute("/{room:path}", silent_ws)])
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)

    try:
        async with httpx.AsyncClient() as client:
            state = AppState(
                client=client,
                server_url=f"ws://127.0.0.1:{port}",
                sync_timeout=1.0,
            )
            with pytest.raises(TimeoutError, match="timed out"):
                await state.connect("test-room")
            # Connection should be cleaned up — no leaked rooms
            assert "test-room" not in state.rooms
    finally:
        srv.should_exit = True
        await asyncio.sleep(0.5)
        task.cancel()


async def test_connect_fails_fast_when_server_down():
    """AppState.connect raises ConnectionError quickly when server is unreachable."""
    import time

    async with httpx.AsyncClient() as client:
        state = AppState(
            client=client,
            server_url="ws://127.0.0.1:1",  # nothing listening
            sync_timeout=10.0,  # should NOT wait this long
        )
        start = time.monotonic()
        with pytest.raises(ConnectionError, match="Connection to room"):
            await state.connect("unreachable-room")
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"Should fail fast, took {elapsed:.1f}s"
        assert "unreachable-room" not in state.rooms


async def test_reconnect_after_server_restart(tmp_path):
    """MCP AppState reconnects transparently after the ASGI server restarts."""
    db_path = tmp_path / "reconnect.db"
    (tmp_path / "index.html").write_text("<html></html>")
    port = get_free_port()

    # Server 1: start, connect MCP, write content
    app1 = create_app(db_path=db_path, public_dir=tmp_path)
    config1 = uvicorn.Config(app1, host="127.0.0.1", port=port, log_level="error")
    srv1 = uvicorn.Server(config1)
    task1 = asyncio.create_task(srv1.serve())
    await asyncio.sleep(1)

    async with httpx.AsyncClient() as client:
        state = AppState(client=client, server_url=f"ws://127.0.0.1:{port}")

        conn1 = await state.connect("reconnect-test")
        conn1.text += "# Before Restart\n\nOriginal content.\n"
        await asyncio.sleep(0.5)

        # Stop server 1
        srv1.should_exit = True
        await asyncio.sleep(0.5)
        task1.cancel()
        await asyncio.sleep(0.5)

        # Server 2: same db_path, same port — simulates a restart
        app2 = create_app(db_path=db_path, public_dir=tmp_path)
        config2 = uvicorn.Config(app2, host="127.0.0.1", port=port, log_level="error")
        srv2 = uvicorn.Server(config2)
        task2 = asyncio.create_task(srv2.serve())
        await asyncio.sleep(1)

        # Connect again — should detect dead connection and reconnect
        conn2 = await state.connect("reconnect-test")
        assert conn2 is not conn1  # fresh connection, not the dead one
        content = str(conn2.text)
        assert "Before Restart" in content, f"Expected persisted content, got: {content!r}"

        await state.close_all()
        srv2.should_exit = True
        await asyncio.sleep(0.5)
        task2.cancel()
