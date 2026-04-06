"""Tests for ASGI server."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import uvicorn
from httpx_ws import aconnect_ws

from tafelmusik.asgi_server import COMMENT_MSG_TYPE, create_app
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


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists(),
    reason="/proc/self/fd not available (Linux-only)",
)
async def test_api_rooms_does_not_leak_fds(server):
    """Repeated /api/rooms calls must not leak SQLite connections.

    Regression test: sqlite3's ``with conn:`` is a transaction manager,
    not a resource manager — it does NOT close the connection. Without
    explicit conn.close(), every /api/rooms call leaked one file descriptor
    via _query_persisted_rooms, exhausting the FD limit in ~10 minutes.
    """
    import os
    pid = os.getpid()

    def count_fds():
        return len(os.listdir(f"/proc/{pid}/fd"))

    baseline = count_fds()

    async with httpx.AsyncClient() as client:
        for _ in range(50):
            r = await client.get(f"http://127.0.0.1:{server}/api/rooms")
            assert r.status_code == 200

    # Allow a small margin (event loop internals), but 50 leaked FDs would be
    # unmistakable. Before the fix, this leaked exactly 50.
    leaked = count_fds() - baseline
    assert leaked < 10, f"Leaked {leaked} FDs after 50 /api/rooms calls"


async def test_room_connections_dont_register_sessions(managed_server):
    """Room WebSocket connections don't touch the session registry."""
    port, manager = managed_server

    async with connect_peer(port, "no-session-test"):
        assert len(manager.session_registry) == 0


async def test_session_comment_delivered(managed_server):
    """POST /api/sessions/{id}/comments stores and delivers to the session."""
    port, manager = managed_server
    sid = "comment-target-session"

    async with httpx.AsyncClient() as ws_client:
        async with aconnect_ws(
            f"http://127.0.0.1:{port}/_ws/_session/{sid}", ws_client,
        ):
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient() as post_client:
                r = await post_client.post(
                    f"http://127.0.0.1:{port}/api/sessions/{sid}/comments",
                    json={"author": "sameer", "body": "hey Claude, look at this"},
                )
            assert r.status_code == 201
            data = r.json()
            assert data["target"] == f"session:{sid}"
            assert data["body"] == "hey Claude, look at this"
            assert data["author"] == "sameer"


async def test_session_comment_404_when_not_connected(managed_server):
    """POST to a non-existent session returns 404."""
    port, _ = managed_server
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://127.0.0.1:{port}/api/sessions/no-such-session/comments",
            json={"author": "sameer", "body": "hello?"},
        )
    assert r.status_code == 404
    assert "not connected" in r.json()["error"]


async def test_session_comment_not_broadcast_to_room(managed_server):
    """Session-direct comments don't leak to room peers."""
    port, manager = managed_server
    sid = "isolated-session"

    # Connect a dedicated session WebSocket and a room peer
    async with httpx.AsyncClient() as ws_client:
        async with aconnect_ws(
            f"http://127.0.0.1:{port}/_ws/_session/{sid}", ws_client,
        ):
            await asyncio.sleep(0.3)
            async with connect_peer(port, "shared-room") as room_text:
                async with httpx.AsyncClient() as post_client:
                    r = await post_client.post(
                        f"http://127.0.0.1:{port}/api/sessions/{sid}/comments",
                        json={"author": "sameer", "body": "session-only msg"},
                    )
                assert r.status_code == 201
                # Room peer's CRDT text should not contain the comment
                await asyncio.sleep(0.3)
                assert "session-only msg" not in str(room_text)


async def test_dedicated_session_websocket(managed_server):
    """Dedicated session WebSocket receives 0x01 comment events."""
    port, manager = managed_server
    sid = "dedicated-ws-test"

    async with httpx.AsyncClient() as client:
        async with aconnect_ws(
            f"http://127.0.0.1:{port}/_ws/_session/{sid}", client,
        ) as ws:
            # Wait for registration
            await asyncio.sleep(0.3)
            assert sid in manager.session_registry

            # POST a session comment
            r = await client.post(
                f"http://127.0.0.1:{port}/api/sessions/{sid}/comments",
                json={"author": "sameer", "body": "direct delivery"},
            )
            assert r.status_code == 201

            # Read the 0x01 event from the WebSocket
            msg = await asyncio.wait_for(ws.receive_bytes(), timeout=2.0)
            assert msg[0:1] == COMMENT_MSG_TYPE
            event = json.loads(msg[1:])
            assert event["type"] == "comment_created"
            assert event["comment"]["body"] == "direct delivery"
            assert event["comment"]["target"] == f"session:{sid}"

    # After disconnect
    await asyncio.sleep(0.3)
    assert sid not in manager.session_registry
