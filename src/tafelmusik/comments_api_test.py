"""Tests for comment HTTP endpoints and WebSocket broadcast."""

import asyncio
import json

import httpx
import pytest
import uvicorn

from tafelmusik.asgi_server import COMMENT_MSG_TYPE, create_app
from tafelmusik.conftest import connect_peer, get_free_port


@pytest.fixture
async def comment_server(tmp_path):
    """Start server for comment endpoint testing."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    app = create_app(
        db_path=tmp_path / "test.db",
        public_dir=tmp_path,
        docs_dir=str(docs_dir),
    )
    (tmp_path / "index.html").write_text("<html></html>")

    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)
    yield port, app
    srv.should_exit = True
    await asyncio.sleep(0.5)
    task.cancel()


async def test_create_and_list_comments(comment_server):
    """POST creates a comment, GET retrieves it."""
    port, app = comment_server
    room = "test-doc"

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post(
            f"/api/rooms/{room}/comments",
            json={"author": "sameer", "body": "Great point"},
        )
        assert r.status_code == 201
        comment = r.json()
        assert comment["author"] == "sameer"
        assert comment["body"] == "Great point"
        assert comment["target"] == room

        r = await client.get(f"/api/rooms/{room}/comments")
        assert r.status_code == 200
        comments = r.json()
        assert len(comments) == 1
        assert comments[0]["id"] == comment["id"]


async def test_create_comment_with_quote_computes_context(comment_server):
    """POST with quote + live room computes prefix/suffix from document."""
    port, app = comment_server
    room = "context-test"

    async with connect_peer(port, room) as text:
        text += "# Introduction\n\nThis is the first paragraph.\n\nSecond."
        await asyncio.sleep(0.5)

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}"
        ) as client:
            r = await client.post(
                f"/api/rooms/{room}/comments",
                json={
                    "author": "sameer",
                    "body": "Needs work",
                    "quote": "first paragraph",
                },
            )
            assert r.status_code == 201
            comment = r.json()
            # Server should compute context from live document
            assert comment["prefix"] is not None
            assert comment["suffix"] is not None


async def test_list_comments_with_anchor_positions(comment_server):
    """GET returns computed anchor positions when room is live."""
    port, app = comment_server
    room = "anchor-test"

    async with connect_peer(port, room) as text:
        text += "Hello world, this is a test document."
        await asyncio.sleep(0.5)

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}"
        ) as client:
            # Create comment with a quote
            await client.post(
                f"/api/rooms/{room}/comments",
                json={
                    "author": "sameer",
                    "body": "Check this",
                    "quote": "test document",
                },
            )

            # List — should include anchor positions
            r = await client.get(f"/api/rooms/{room}/comments")
            comments = r.json()
            assert len(comments) == 1
            assert comments[0]["anchor"] is not None
            assert comments[0]["anchor"]["start"] >= 0
            assert comments[0]["anchor"]["confident"] is True


async def test_resolve_comment(comment_server):
    """Resolving a comment marks it resolved and filters from default list."""
    port, app = comment_server
    room = "resolve-test"

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post(
            f"/api/rooms/{room}/comments",
            json={"author": "sameer", "body": "Fix this"},
        )
        comment_id = r.json()["id"]

        r = await client.post(
            f"/api/rooms/{room}/comments/{comment_id}/resolve"
        )
        assert r.status_code == 200
        assert r.json()["resolved"] is True

        # Default list excludes resolved
        r = await client.get(f"/api/rooms/{room}/comments")
        assert len(r.json()) == 0

        # Include resolved
        r = await client.get(f"/api/rooms/{room}/comments?resolved=true")
        assert len(r.json()) == 1


async def test_resolve_nonexistent_returns_404(comment_server):
    """Resolving a non-existent comment returns 404."""
    port, app = comment_server

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post(
            "/api/rooms/any-room/comments/fake-id/resolve"
        )
        assert r.status_code == 404


async def test_comments_isolated_by_room(comment_server):
    """Comments for different rooms don't leak across."""
    port, app = comment_server

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        await client.post(
            "/api/rooms/room-a/comments",
            json={"author": "sameer", "body": "Comment A"},
        )
        await client.post(
            "/api/rooms/room-b/comments",
            json={"author": "sameer", "body": "Comment B"},
        )

        r = await client.get("/api/rooms/room-a/comments")
        assert len(r.json()) == 1
        assert r.json()[0]["body"] == "Comment A"

        r = await client.get("/api/rooms/room-b/comments")
        assert len(r.json()) == 1
        assert r.json()[0]["body"] == "Comment B"


async def test_nested_room_path_in_url(comment_server):
    """Room names with slashes work in comment URLs."""
    port, app = comment_server
    room = "batterie/tafelmusik/docs/readme"

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post(
            f"/api/rooms/{room}/comments",
            json={"author": "sameer", "body": "Nested room"},
        )
        assert r.status_code == 201
        assert r.json()["target"] == room

        r = await client.get(f"/api/rooms/{room}/comments")
        assert len(r.json()) == 1


async def test_comment_broadcast_to_websocket_peer(comment_server):
    """Creating a comment broadcasts 0x01 event to WebSocket peers."""
    port, app = comment_server
    room = "broadcast-test"
    received = []

    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}"
    ) as client:
        from httpx_ws import aconnect_ws

        async with aconnect_ws(
            f"http://127.0.0.1:{port}/_ws/{room}", client
        ) as ws:
            # Drain initial sync message
            await ws.receive_bytes()
            await asyncio.sleep(0.5)

            # Listen for comment events in background
            async def listen():
                try:
                    while True:
                        msg = await asyncio.wait_for(
                            ws.receive_bytes(), timeout=3.0
                        )
                        if msg[0:1] == COMMENT_MSG_TYPE:
                            received.append(msg)
                except (TimeoutError, Exception):
                    pass

            listener = asyncio.create_task(listen())

            # POST a comment
            r = await client.post(
                f"/api/rooms/{room}/comments",
                json={"author": "sameer", "body": "Hello from HTTP"},
            )
            assert r.status_code == 201

            await asyncio.sleep(1)
            listener.cancel()

    assert len(received) >= 1
    event = json.loads(received[0][1:])
    assert event["type"] == "comment_created"
    assert event["comment"]["body"] == "Hello from HTTP"
