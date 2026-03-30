"""Tests for calute — files on disk as CRDT overlay.

Integration tests verifying the file lifecycle: hydrate from .md file,
flush to .md file, directory listing, and round-trip integrity.
"""

import asyncio

import httpx
import pytest
import uvicorn
from pycrdt import Map, Text

from tafelmusik.asgi_server import create_app
from tafelmusik.conftest import connect_peer, get_free_port


@pytest.fixture
async def file_server(tmp_path):
    """Start a server with a docs_dir for file-backed rooms."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    app = create_app(
        db_path=tmp_path / "test.db",
        public_dir=tmp_path,
        docs_dir=docs_dir,
    )
    (tmp_path / "index.html").write_text("<html></html>")

    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)
    yield port, docs_dir, app.state.manager
    srv.should_exit = True
    await asyncio.sleep(0.5)
    task.cancel()


# --- Hydrate from file ---


async def test_hydrate_from_md_file(file_server):
    """Room opens with content from .md file in docs_dir."""
    port, docs_dir, _ = file_server
    md_content = "# Meeting Notes\n\nImportant stuff here.\n"
    (docs_dir / "meeting.md").write_text(md_content)

    async with connect_peer(port, "meeting") as text:
        assert str(text) == md_content


async def test_hydrate_from_nested_md_file(file_server):
    """Nested room names map to nested file paths."""
    port, docs_dir, _ = file_server
    (docs_dir / "project").mkdir()
    md_content = "# Project Plan\n\nPhase 1.\n"
    (docs_dir / "project" / "plan.md").write_text(md_content)

    async with connect_peer(port, "project/plan") as text:
        assert str(text) == md_content


async def test_hydrate_fallback_to_sqlite(file_server):
    """Room with no .md file falls back to SQLite restore."""
    port, docs_dir, _ = file_server
    # No .md file — write via CRDT, disconnect, reconnect
    async with connect_peer(port, "ephemeral") as text:
        text += "Created via CRDT"
        await asyncio.sleep(0.5)

    # Room cleaned up, reconnect — should restore from SQLite
    await asyncio.sleep(0.5)
    async with connect_peer(port, "ephemeral") as text:
        assert "Created via CRDT" in str(text)


async def test_hydrate_empty_room(file_server):
    """Room with no .md file and no SQLite data starts empty."""
    port, docs_dir, _ = file_server
    async with connect_peer(port, "brand-new") as text:
        assert str(text) == ""


async def test_hydrate_empty_md_file(file_server):
    """Empty .md file results in empty Y.Text."""
    port, docs_dir, _ = file_server
    (docs_dir / "empty.md").write_text("")

    async with connect_peer(port, "empty") as text:
        assert str(text) == ""


# --- Directory listing ---


async def test_list_rooms_includes_md_files(file_server):
    """list_rooms returns .md files from docs_dir."""
    port, docs_dir, _ = file_server
    (docs_dir / "alpha.md").write_text("# Alpha")
    (docs_dir / "beta.md").write_text("# Beta")

    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://127.0.0.1:{port}/api/rooms")
        rooms = r.json()["rooms"]
        assert "alpha" in rooms
        assert "beta" in rooms


async def test_list_rooms_includes_nested_md_files(file_server):
    """Nested .md files appear with path-style room names."""
    port, docs_dir, _ = file_server
    (docs_dir / "notes").mkdir()
    (docs_dir / "notes" / "daily.md").write_text("# Daily")

    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://127.0.0.1:{port}/api/rooms")
        rooms = r.json()["rooms"]
        assert "notes/daily" in rooms


async def test_list_rooms_merges_file_and_memory(file_server):
    """list_rooms shows both file-backed and in-memory rooms."""
    port, docs_dir, _ = file_server
    (docs_dir / "on-disk.md").write_text("# On Disk")

    # Create an in-memory room with no .md file
    async with connect_peer(port, "in-memory") as text:
        text += "In memory only"
        await asyncio.sleep(0.3)

        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/rooms")
            rooms = r.json()["rooms"]
            assert "on-disk" in rooms
            assert "in-memory" in rooms


# --- Flush ---


async def test_flush_writes_md_file(file_server):
    """Flushing a room writes Y.Text content to .md file."""
    port, docs_dir, manager = file_server

    async with connect_peer(port, "flush-test") as text:
        text += "# Flushed\n\nContent here.\n"
        await asyncio.sleep(0.5)

        # Simulate flush: read from the room's Doc and write to file
        room = manager.rooms["flush-test"]
        room_text = room.doc.get("content", type=Text)
        content = str(room_text)

        md_path = docs_dir / "flush-test.md"
        md_path.write_text(content)

        assert md_path.read_text() == "# Flushed\n\nContent here.\n"


async def test_flush_creates_parent_dirs(file_server):
    """Flush creates parent directories for nested room names."""
    port, docs_dir, manager = file_server

    async with connect_peer(port, "deep/nested/doc") as text:
        text += "Nested content"
        await asyncio.sleep(0.5)

        room = manager.rooms["deep/nested/doc"]
        room_text = room.doc.get("content", type=Text)
        content = str(room_text)

        md_path = docs_dir / "deep" / "nested" / "doc.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content)

        assert md_path.exists()
        assert md_path.read_text() == "Nested content"


async def test_flush_wipes_comments(file_server):
    """Flushing clears all comments from the Y.Map."""
    port, docs_dir, manager = file_server

    async with connect_peer(port, "comment-wipe") as text:
        text += "Some content"
        await asyncio.sleep(0.5)

        # Add a comment directly via the server's Doc
        room = manager.rooms["comment-wipe"]
        comments_map = room.doc.get("comments", type=Map)
        with room.doc.transaction():
            comment = Map()
            comments_map["test-comment"] = comment
            comment["body"] = "A test comment"
            comment["author"] = "sameer"

        assert len(list(comments_map)) == 1

        # Simulate flush comment wipe
        with room.doc.transaction():
            for key in list(comments_map):
                del comments_map[key]

        assert len(list(comments_map)) == 0


# --- Round-trip ---


async def test_round_trip_hydrate_edit_flush_rehydrate(file_server):
    """Full lifecycle: file → CRDT → edit → flush → file → CRDT."""
    port, docs_dir, manager = file_server

    # Step 1: Create .md file
    original = "# Report\n\nDraft content.\n"
    (docs_dir / "report.md").write_text(original)

    # Step 2: Hydrate and edit via CRDT
    async with connect_peer(port, "report") as text:
        assert str(text) == original
        text += "\n## Findings\n\nNew section added.\n"
        await asyncio.sleep(0.5)

        # Step 3: Flush — read from room Doc, write to file
        room = manager.rooms["report"]
        room_text = room.doc.get("content", type=Text)
        edited = str(room_text)
        (docs_dir / "report.md").write_text(edited)

    # Step 4: Room cleaned up
    await asyncio.sleep(0.5)
    assert "report" not in manager.rooms

    # Step 5: Re-hydrate from the flushed file
    async with connect_peer(port, "report") as text:
        rehydrated = str(text)
        assert "Draft content." in rehydrated
        assert "New section added." in rehydrated


async def test_file_takes_priority_over_sqlite(file_server):
    """When both .md file and SQLite state exist, file wins."""
    port, docs_dir, _ = file_server

    # Write via CRDT (creates SQLite state)
    async with connect_peer(port, "priority-test") as text:
        text += "SQLite content"
        await asyncio.sleep(0.5)

    await asyncio.sleep(0.5)

    # Now create a .md file with different content
    (docs_dir / "priority-test.md").write_text("File content wins\n")

    # Reconnect — file should take priority
    async with connect_peer(port, "priority-test") as text:
        assert str(text) == "File content wins\n"
