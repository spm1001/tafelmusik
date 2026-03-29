"""Tests for ASGI server."""

import asyncio

import httpx
import pytest
import uvicorn

from tafelmusik.asgi_server import create_app
from tafelmusik.conftest import connect_peer, get_free_port


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


@pytest.fixture
async def managed_server(tmp_path):
    """Start a server and expose its RoomManager for lifecycle assertions."""
    app = create_app(db_path=tmp_path / "test.db", public_dir=tmp_path)
    (tmp_path / "index.html").write_text("<html></html>")

    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)
    yield port, app.state.manager
    srv.should_exit = True
    await asyncio.sleep(0.5)
    task.cancel()


async def test_two_clients_sync(server):
    """Two clients editing the same room see each other's changes."""
    async with connect_peer(server, "sync-test") as t1:
        async with connect_peer(server, "sync-test") as t2:
            t1 += "Hello from client 1"
            await asyncio.sleep(0.5)
            assert "Hello from client 1" in str(t2)

            t2 += "\nHello from client 2"
            await asyncio.sleep(0.5)
            assert "Hello from client 2" in str(t1)


async def test_multi_room(server):
    """Different rooms are independent."""
    # Write to room-a
    async with connect_peer(server, "room-a") as ta:
        ta += "Content A"
        await asyncio.sleep(0.5)

    # Write to room-b
    async with connect_peer(server, "room-b") as tb:
        tb += "Content B"
        await asyncio.sleep(0.5)

    # Read room-a — should not contain room-b's content
    async with connect_peer(server, "room-a") as text:
        content = str(text)
        assert "Content A" in content
        assert "Content B" not in content


async def test_http_serves_index(server):
    """HTTP GET / serves index.html."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://127.0.0.1:{server}/")
        assert r.status_code == 200
        assert "<html>" in r.text


async def test_server_survives_send_after_disconnect(server):
    """Server handles send to a disconnected client without crashing.

    Starlette's WebSocket state machine raises WebSocketDisconnect on the
    first failed send, then RuntimeError on subsequent sends. Both must be
    caught in StarletteWebsocket.send() to prevent crashing the entire
    WebsocketServer via pycrdt's task group.
    """
    # Client 1: connect, write content, then disconnect
    async with connect_peer(server, "send-crash-test") as t1:
        t1 += "Content from client 1"
        await asyncio.sleep(0.5)
    # WebSocket closed — server may still try to send to this dead channel

    await asyncio.sleep(0.5)

    # Client 2: connect to the SAME room — proves server is still alive
    async with connect_peer(server, "send-crash-test") as t2:
        assert "Content from client 1" in str(t2)

    # Client 3: connect to a DIFFERENT room — proves entire server is healthy
    async with connect_peer(server, "unrelated-room") as t3:
        t3 += "Still alive"
        await asyncio.sleep(0.3)
        assert "Still alive" in str(t3)


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

    async with connect_peer(port, "persist-test") as text:
        text += "# Persistence\n\nSurvives restart."
        await asyncio.sleep(0.5)

    srv1.should_exit = True
    await asyncio.sleep(0.5)
    task1.cancel()

    # Server 2: fresh app, same db_path — content should be restored
    app2 = create_app(db_path=db_path, public_dir=tmp_path)
    config2 = uvicorn.Config(app2, host="127.0.0.1", port=port, log_level="error")
    srv2 = uvicorn.Server(config2)
    task2 = asyncio.create_task(srv2.serve())
    await asyncio.sleep(1)

    async with connect_peer(port, "persist-test") as text:
        result = str(text)
        assert "Survives restart." in result, f"Expected persisted content, got: {result!r}"

    srv2.should_exit = True
    await asyncio.sleep(0.5)
    task2.cancel()


async def test_room_lifecycle_cleanup_and_restore(managed_server):
    """Connect → write → disconnect → room cleaned up → reconnect → data restored."""
    port, manager = managed_server
    room_name = "lifecycle-test"

    # Connect, write, disconnect
    async with connect_peer(port, room_name) as text:
        text += "# Lifecycle\n\nSurvives cleanup."
        await asyncio.sleep(0.5)

    # Room should be cleaned up after last client disconnects
    await asyncio.sleep(0.5)
    assert room_name not in manager.rooms, "Room should be removed after last disconnect"

    # Reconnect — data should be restored from SQLite
    async with connect_peer(port, room_name) as text:
        result = str(text)
        assert "Survives cleanup." in result, f"Expected restored content, got: {result!r}"


async def test_room_stays_with_remaining_clients(managed_server):
    """Room stays alive while clients remain; cleans up when last one leaves."""
    port, manager = managed_server
    room_name = "multi-client-test"

    async with connect_peer(port, room_name) as t1:
        t1 += "Content from client 1"
        await asyncio.sleep(0.5)

        # Second client connects to same room
        async with connect_peer(port, room_name) as t2:
            await asyncio.sleep(0.3)
            assert "Content from client 1" in str(t2)

        # Client 2 disconnected — room should stay (client 1 still connected)
        await asyncio.sleep(0.3)
        assert room_name in manager.rooms, "Room should stay with client 1 still connected"

    # Both disconnected — room should be cleaned up
    await asyncio.sleep(0.5)
    assert room_name not in manager.rooms, "Room should be removed after last disconnect"
