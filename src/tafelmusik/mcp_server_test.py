"""Tests for MCP server — integration tests against a real ASGI server.

Tests the AppState/RoomConnection connection management and tool logic
by connecting to a live server instance.  Also unit tests for _sync_loop.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from anyio import Event
from pycrdt import Doc, Text, YMessageType, YSyncMessageType

from tafelmusik import authors
from tafelmusik.asgi_server import create_app
from tafelmusik.conftest import MockChannel, connect_peer, get_free_port
from tafelmusik.mcp_server import AppState, _send_channel_notification, _sync_loop


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
async def make_state(port, poll_interval=None, **kwargs):
    """Create an AppState with setup and teardown in the same task.

    Using a context manager (not a fixture) ensures that the anyio cancel
    scopes inside aconnect_ws are entered and exited from the same asyncio
    Task. Pytest-asyncio fixture teardown can run in a different task context.

    Pass poll_interval to start the room poller (disabled by default in tests).
    """
    async with httpx.AsyncClient() as client:
        state = AppState(
            client=client,
            server_url=f"ws://127.0.0.1:{port}",
            keepalive=None,  # No keepalive in tests
            **kwargs,
        )
        if poll_interval is not None:
            await state.start_room_poller(interval=poll_interval)
        try:
            yield state
        finally:
            await state.close_all()


# --- Unit tests for _sync_loop ---


async def test_sync_loop_handshake():
    """_sync_loop sends SYNC_STEP1 and sets synced on SYNC_STEP2."""
    server_doc = Doc()
    server_doc["content"] = server_text = Text()
    server_text += "Server content"

    client_doc = Doc()
    client_doc["content"] = client_text = Text()

    synced = Event()
    channel = MockChannel(peer_doc=server_doc)

    task = asyncio.create_task(_sync_loop(client_doc, channel, synced, keepalive=None))
    await asyncio.wait_for(synced.wait(), timeout=2.0)

    assert synced.is_set()
    assert "Server content" in str(client_text)
    # First message sent should be SYNC (type byte 0)
    assert channel.sent[0][0] == YMessageType.SYNC

    channel.close()
    await asyncio.wait_for(task, timeout=2.0)


async def test_sync_loop_broadcasts_local_changes():
    """Local doc changes are sent as SYNC_UPDATE messages."""
    server_doc = Doc()
    server_doc["content"] = Text()

    client_doc = Doc()
    client_doc["content"] = client_text = Text()

    synced = Event()
    channel = MockChannel(peer_doc=server_doc)

    task = asyncio.create_task(_sync_loop(client_doc, channel, synced, keepalive=None))
    await asyncio.wait_for(synced.wait(), timeout=2.0)

    initial_sent = len(channel.sent)
    client_text += "New local content"
    await asyncio.sleep(0.1)  # Let _send_updates pick it up

    assert len(channel.sent) > initial_sent
    update_msg = channel.sent[-1]
    assert update_msg[0] == YMessageType.SYNC
    assert YSyncMessageType(update_msg[1]) == YSyncMessageType.SYNC_UPDATE

    channel.close()
    await asyncio.wait_for(task, timeout=2.0)


async def test_sync_loop_exits_on_channel_close():
    """_sync_loop returns when the channel iterator ends."""
    doc = Doc()
    doc["content"] = Text()

    synced = Event()
    channel = MockChannel(peer_doc=Doc())

    task = asyncio.create_task(_sync_loop(doc, channel, synced, keepalive=None))
    await asyncio.wait_for(synced.wait(), timeout=2.0)

    channel.close()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


async def test_sync_loop_keepalive_detects_dead():
    """Keepalive probe detects unresponsive connection."""
    server_doc = Doc()
    server_doc["content"] = Text()

    class SilentAfterSync(MockChannel):
        """Responds to initial sync, then ignores everything."""

        def __init__(self, peer_doc):
            super().__init__(peer_doc)
            self._synced = False

        async def send(self, message: bytes) -> None:
            self.sent.append(message)
            if not self._synced and self._peer_doc is not None:
                if message[0] == YMessageType.SYNC:
                    from pycrdt import handle_sync_message

                    reply = handle_sync_message(message[1:], self._peer_doc)
                    if reply is not None:
                        self._queue.put_nowait(reply)
                        self._synced = True

    doc = Doc()
    doc["content"] = Text()
    synced = Event()
    channel = SilentAfterSync(peer_doc=server_doc)

    # keepalive=0.3 → probe at 0.3s, wait 0.3s for response → dead at ~0.6s
    task = asyncio.create_task(_sync_loop(doc, channel, synced, keepalive=0.3))
    await asyncio.wait_for(synced.wait(), timeout=2.0)

    # Task should exit on its own when keepalive detects the dead connection
    await asyncio.wait_for(task, timeout=3.0)
    assert task.done()

    # Should have sent at least one keepalive probe (a second SYNC_STEP1)
    sync_step1_count = sum(
        1
        for msg in channel.sent
        if msg[0] == YMessageType.SYNC and YSyncMessageType(msg[1]) == YSyncMessageType.SYNC_STEP1
    )
    assert sync_step1_count >= 2, "Expected initial SYNC_STEP1 + at least one probe"


async def test_subtask_sends_reach_server(server):
    """Verify that _send_updates (anyio subtask) can send on the parent's WebSocket.

    _sync_loop uses create_task_group: the receive loop runs in the host task
    (same asyncio Task as aconnect_ws), while _send_updates runs in a subtask
    (separate asyncio Task).  This test proves that writes from the subtask
    actually reach the server and are visible to another client — i.e., the
    anyio structured concurrency contract lets subtasks do I/O on the parent's
    WebSocket without cancel scope violations.
    """
    async with make_state(server) as state:
        conn = await state.connect("subtask-send-test")
        conn.text += "Written via _send_updates subtask"
        await asyncio.sleep(0.5)

    # Second client reads — if the subtask send failed, this would be empty
    async with connect_peer(server, "subtask-send-test") as text:
        assert "Written via _send_updates subtask" in str(text)


# --- Integration tests ---


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
        conn = await state.connect("test-mcp-to-browser")
        conn.text += "# From Claude\n\nThis was written by the MCP server.\n"
        await asyncio.sleep(0.5)

        async with connect_peer(server, "test-mcp-to-browser") as text:
            assert "From Claude" in str(text)
            assert "written by the MCP server" in str(text)


async def test_browser_write_visible_to_mcp(server):
    """Content written by a separate pycrdt client is readable via MCP."""
    async with connect_peer(server, "test-browser-to-mcp") as text:
        text += "# From Browser\n\nSameer typed this.\n"
        await asyncio.sleep(0.5)

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

        document.replace_section(
            conn.text, "## API\n\nNew API documentation\n", author=authors.TEST
        )
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

        document.replace_all(conn.text, "# Fresh Start\n\nCompletely new.\n", author=authors.TEST)
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
    async with connect_peer(server, "listed-room") as text:
        text += "Some content"
        await asyncio.sleep(0.5)

        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{server}/api/rooms")
            assert response.status_code == 200
            data = response.json()
            assert "listed-room" in data["rooms"]


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
                keepalive=None,
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
            keepalive=None,
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
        state = AppState(
            client=client,
            server_url=f"ws://127.0.0.1:{port}",
            keepalive=None,
        )

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


# --- Observer tests: change_queue and origin filtering ---


async def test_observer_notifies_on_remote_edit(server):
    """Browser edits produce pending notifications after debounce."""
    async with make_state(server) as state:
        conn = await state.connect("observer-test")
        assert len(conn.pending_notifications) == 0

        # Simulate a browser edit via connect_peer
        async with connect_peer(server, "observer-test") as browser:
            browser += "Hello from Sameer"
            await asyncio.sleep(0.5)

        # Wait for debounce (2s) + margin
        await asyncio.sleep(3.0)
        assert len(conn.pending_notifications) > 0


async def test_observer_skips_claude_edits(server):
    """Claude's own edits (origin='claude') produce no notifications."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("self-filter-test")

        # Claude writes — should produce no notification
        document.replace_all(
            conn.text, "# Claude's doc\n\nWritten by Claude.\n", author=authors.CLAUDE
        )

        # Wait longer than debounce to confirm nothing arrives
        await asyncio.sleep(3.0)
        assert len(conn.pending_notifications) == 0, "Claude's edit should not notify"


async def test_observer_notifies_remote_but_not_claude(server):
    """Mixed scenario: Claude writes, then browser writes. Only browser edit notifies."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("mixed-test")

        # Claude writes first
        document.replace_all(conn.text, "## Draft\n\nClaude's content.\n", author=authors.CLAUDE)
        await asyncio.sleep(3.0)
        assert len(conn.pending_notifications) == 0

        # Browser edits
        async with connect_peer(server, "mixed-test") as browser:
            browser += "\nSameer added this."
            await asyncio.sleep(0.5)

        # Wait for debounce
        await asyncio.sleep(3.0)
        assert len(conn.pending_notifications) > 0


async def test_consumer_debounces_and_diffs(server):
    """The change consumer debounces rapid edits and produces section-level diffs."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("debounce-test")

        # Claude writes initial content with sections
        document.replace_all(
            conn.text,
            "## API\n\nOriginal API docs.\n\n## Usage\n\nUsage text.\n",
            author=authors.CLAUDE,
        )
        await asyncio.sleep(0.5)
        assert conn.change_queue.empty()  # Claude's edit not queued

        # Browser edits the API section
        async with connect_peer(server, "debounce-test") as browser:
            # Find and replace the API section content
            content = str(browser)
            api_end = content.index("## Usage")
            del browser[0:api_end]
            browser.insert(0, "## API\n\nBrowser rewrote API.\n\n")
            await asyncio.sleep(0.5)

        # Wait for debounce (2s default) + margin
        await asyncio.sleep(3.0)

        # Consumer should have processed the change into pending_notifications
        assert len(conn.pending_notifications) > 0
        changes = conn.pending_notifications[0]
        headings = [h for h, _ in changes]
        assert "## API" in headings


async def test_consumer_coalesces_rapid_edits(server):
    """Multiple rapid edits within the debounce window produce one notification."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("coalesce-test")

        document.replace_all(
            conn.text,
            "## Notes\n\nOriginal notes.\n",
            author=authors.CLAUDE,
        )
        await asyncio.sleep(0.5)

        # Browser sends 5 rapid edits (within debounce window)
        async with connect_peer(server, "coalesce-test") as browser:
            for i in range(5):
                browser += f"\nEdit {i}"
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.3)

        # Wait for debounce
        await asyncio.sleep(3.0)

        # Should produce exactly 1 notification, not 5
        assert len(conn.pending_notifications) == 1


# --- TCP partition proxy for keepalive integration test ---


@asynccontextmanager
async def _partition_proxy(target_port):
    """TCP proxy that simulates network partition.

    Yields (proxy_port, partition_fn, heal_fn). While partitioned,
    silently drops all traffic without closing TCP connections — the
    client sees silence, not an error.
    """
    proxy_port = get_free_port()
    dropping = [False]
    relay_tasks = []

    async def _relay(src, dst):
        try:
            while True:
                data = await src.read(8192)
                if not data:
                    break
                if not dropping[0]:
                    dst.write(data)
                    await dst.drain()
        except (ConnectionResetError, BrokenPipeError, OSError, asyncio.CancelledError):
            pass

    async def _handle(client_reader, client_writer):
        try:
            target_reader, target_writer = await asyncio.open_connection(
                "127.0.0.1", target_port
            )
        except OSError:
            client_writer.close()
            return
        relay_tasks.append(asyncio.create_task(_relay(client_reader, target_writer)))
        relay_tasks.append(asyncio.create_task(_relay(target_reader, client_writer)))

    srv = await asyncio.start_server(_handle, "127.0.0.1", proxy_port)

    def partition():
        dropping[0] = True

    def heal():
        dropping[0] = False

    try:
        yield proxy_port, partition, heal
    finally:
        for t in relay_tasks:
            t.cancel()
        srv.close()
        await srv.wait_closed()


async def test_keepalive_detects_dead_connection(tmp_path):
    """Keepalive detects a silently dead connection via network partition.

    A TCP proxy between client and server is partitioned mid-session.
    The keepalive probe is sent but dropped — no response comes back.
    The dead event fires within the keepalive window. After healing the
    partition, reconnection restores persisted data.
    """
    import time

    db_path = tmp_path / "keepalive-int.db"
    (tmp_path / "index.html").write_text("<html></html>")
    server_port = get_free_port()

    app = create_app(db_path=db_path, public_dir=tmp_path)
    config = uvicorn.Config(app, host="127.0.0.1", port=server_port, log_level="error")
    srv = uvicorn.Server(config)
    server_task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)

    try:
        async with _partition_proxy(server_port) as (proxy_port, partition, heal):
            async with httpx.AsyncClient() as client:
                state = AppState(
                    client=client,
                    server_url=f"ws://127.0.0.1:{proxy_port}",
                    keepalive=0.5,
                )

                # Connect and write through proxy
                conn = await state.connect("keepalive-test")
                conn.text += "# Keepalive\n\nSurvives partition.\n"
                await asyncio.sleep(0.5)

                # Partition — keepalive should detect within ~1s
                # (probe at 0.5s, wait min(0.5, 10)=0.5s for response → dead at ~1s)
                start = time.monotonic()
                partition()

                await asyncio.wait_for(conn.dead.wait(), timeout=5.0)
                elapsed = time.monotonic() - start
                assert elapsed < 3.0, f"Dead took {elapsed:.1f}s, expected ~1s"

                # Heal partition, reconnect
                heal()
                conn2 = await state.connect("keepalive-test")
                assert conn2 is not conn
                content = str(conn2.text)
                assert "Survives partition." in content, (
                    f"Expected persisted content, got: {content!r}"
                )

                await state.close_all()
    finally:
        srv.should_exit = True
        await asyncio.sleep(0.5)
        server_task.cancel()


# --- Channel notification tests ---


class MockSession:
    """Captures messages sent via send_message() for assertion."""

    def __init__(self):
        self.messages = []

    async def send_message(self, message):
        self.messages.append(message)


async def test_send_channel_notification_format():
    """_send_channel_notification sends correctly shaped JSONRPCNotification."""
    session = MockSession()
    changes = [("## API", "modified"), ("## New", "added")]

    await _send_channel_notification(session, "test-room", changes)

    assert len(session.messages) == 1
    msg = session.messages[0]
    notification = msg.message.root
    assert notification.method == "notifications/claude/channel"
    assert "test-room" in notification.params["content"]
    assert "Modified section: ## API" in notification.params["content"]
    assert "Added section: ## New" in notification.params["content"]
    assert notification.params["meta"]["room"] == "test-room"


async def test_channel_notification_on_remote_edit(server):
    """Browser edit triggers channel notification when session is captured."""
    from tafelmusik import document

    mock_session = MockSession()

    async with make_state(server) as state:
        state.session = mock_session

        conn = await state.connect("channel-notify-test")

        # Claude writes initial content
        document.replace_all(
            conn.text,
            "## Intro\n\nOriginal.\n\n## Details\n\nSome details.\n",
            author=authors.CLAUDE,
        )
        await asyncio.sleep(0.5)
        assert len(mock_session.messages) == 0  # Claude's edit: no notification

        # Browser edits the Details section
        async with connect_peer(server, "channel-notify-test") as browser:
            content = str(browser)
            details_start = content.index("## Details")
            del browser[details_start:]
            browser.insert(details_start, "## Details\n\nSameer rewrote this.\n")
            await asyncio.sleep(0.5)

        # Wait for debounce (2s) + margin
        await asyncio.sleep(3.0)

        # Channel notification should have been sent
        assert len(mock_session.messages) > 0
        notification = mock_session.messages[0].message.root
        assert notification.method == "notifications/claude/channel"
        assert "channel-notify-test" in notification.params["content"]


async def test_no_channel_notification_without_session(server):
    """Without a captured session, changes go to pending_notifications only."""
    from tafelmusik import document

    async with make_state(server) as state:
        # Don't set state.session — leave it None
        conn = await state.connect("no-session-test")

        document.replace_all(
            conn.text, "## Test\n\nOriginal.\n", author=authors.CLAUDE
        )
        await asyncio.sleep(0.5)

        # Browser edits
        async with connect_peer(server, "no-session-test") as browser:
            browser += "\nBrowser edit"
            await asyncio.sleep(0.5)

        await asyncio.sleep(3.0)

        # pending_notifications should be populated (existing behaviour)
        assert len(conn.pending_notifications) > 0
        # No crash — graceful fallback


# --- Patch mode tests ---


async def test_patch_via_mcp(server):
    """patch() works through the MCP connection layer — surgical find-and-replace."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("test-patch")
        document.replace_all(
            conn.text,
            "The quick brown fox jumps over the lazy dog.",
            author=authors.CLAUDE,
        )
        await asyncio.sleep(0.3)

        document.patch(conn.text, "brown fox", "red fox", author=authors.CLAUDE)
        assert str(conn.text) == "The quick red fox jumps over the lazy dog."
        await asyncio.sleep(0.5)  # Let update propagate to server

    # Verify patch is visible to another client
    async with connect_peer(server, "test-patch") as text:
        assert "red fox" in str(text)
        assert "brown fox" not in str(text)


async def test_patch_preserves_authorship_via_mcp(server):
    """Patch preserves authorship attrs on untouched text through MCP."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("test-patch-authorship")

        # Browser writes content (Sameer's authorship)
        async with connect_peer(server, "test-patch-authorship") as browser:
            with browser.doc.transaction(origin=authors.SAMEER):
                browser.insert(0, "Hello wrold!", attrs={"author": authors.SAMEER})
            await asyncio.sleep(0.5)

        # Wait for sync
        await asyncio.sleep(0.5)

        # Claude patches the typo
        document.patch(conn.text, "wrold", "world", author=authors.CLAUDE)
        assert str(conn.text) == "Hello world!"

        # Check authorship preservation
        diff = conn.text.diff()
        segments = [(val, attrs) for val, attrs in diff]
        assert segments[0] == ("Hello ", {"author": authors.SAMEER})
        assert segments[1] == ("world", {"author": authors.CLAUDE})
        assert segments[2] == ("!", {"author": authors.SAMEER})


async def test_patch_error_no_match_via_mcp(server):
    """patch() raises ValueError when text not found through MCP."""
    from tafelmusik import document

    async with make_state(server) as state:
        conn = await state.connect("test-patch-nomatch")
        conn.text += "Hello world"
        await asyncio.sleep(0.3)

        with pytest.raises(ValueError, match="not found"):
            document.patch(conn.text, "missing", "replacement", author=authors.CLAUDE)
        assert str(conn.text) == "Hello world"  # unchanged


# --- h1 replace_section guard ---


# --- Room poller tests ---


async def test_room_poller_discovers_new_room(server):
    """Room poller connects to a room created by a browser client, without any tool call."""
    mock_session = MockSession()

    async with make_state(server, poll_interval=0.5) as state:
        state.session = mock_session

        # No rooms connected yet
        assert len(state.rooms) == 0

        # Browser creates a room the MCP server has never seen
        async with connect_peer(server, "poller-discovery-test") as browser:
            browser += "# Hello from browser\n\nSameer is writing.\n"
            await asyncio.sleep(0.5)

            # Wait for poller to discover and connect (poll interval 0.5s + sync)
            await asyncio.sleep(2.0)

            # MCP server should now be connected to the room
            assert "poller-discovery-test" in state.rooms
            conn = state.rooms["poller-discovery-test"]
            assert "Hello from browser" in str(conn.text)

            # Now browser edits — observer should fire notification
            browser += "\nAnother edit from Sameer."
            await asyncio.sleep(0.5)

        # Wait for debounce (2s) + margin
        await asyncio.sleep(3.0)

        # Channel notification should have been sent for the browser edit
        assert len(conn.pending_notifications) > 0 or len(mock_session.messages) > 0


async def test_room_poller_survives_server_down():
    """Room poller handles unreachable server gracefully — no crash, retries on next interval."""
    async with httpx.AsyncClient() as client:
        state = AppState(
            client=client,
            server_url="ws://127.0.0.1:1",  # nothing listening
            keepalive=None,
        )
        await state.start_room_poller(interval=0.3)

        # Let the poller run several cycles against a dead server
        await asyncio.sleep(1.5)

        # Poller should still be alive (not crashed)
        assert not state._poll_task.done()
        # No rooms connected (server unreachable)
        assert len(state.rooms) == 0

        await state.close_all()
        # Poller task should be cancelled cleanly
        assert state._poll_task.done()


async def test_room_poller_ignores_already_connected(server):
    """Room poller doesn't reconnect to rooms already in state.rooms."""
    async with make_state(server, poll_interval=0.5) as state:
        # Manually connect to a room first
        conn = await state.connect("already-connected")
        conn.text += "Existing content"
        await asyncio.sleep(0.3)

        # Let poller run — it should see "already-connected" in the room list
        # but NOT create a new connection
        await asyncio.sleep(1.5)

        # Same connection object — not replaced
        assert state.rooms["already-connected"] is conn
        assert "Existing content" in str(conn.text)


def test_heading_level_detection():
    """heading_level correctly identifies heading levels."""
    from tafelmusik.document import heading_level

    assert heading_level("# Title") == 1
    assert heading_level("## Section") == 2
    assert heading_level("### Subsection") == 3
    assert heading_level("Not a heading") is None
    assert heading_level("") is None
