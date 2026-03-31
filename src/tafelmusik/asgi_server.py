"""ASGI server entry point — always-running, holds Y.Doc, serves web editor.

Owns its room management directly using public pycrdt APIs (Doc, sync
functions, SQLiteYStore). No dependency on pycrdt.websocket.
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from pycrdt import (
    Doc,
    Text,
    YMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
)
from pycrdt.store import SQLiteYStore
from pycrdt.store.base import YDocNotFound
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

PORT = 3456
ROOT = Path(__file__).resolve().parent.parent.parent
HOME = Path.home()

# Uvicorn configures its own loggers but not root. basicConfig adds a
# root handler so our lifecycle logs reach stderr (and journalctl).
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
log = logging.getLogger(__name__)


# --- Channel adapter: Starlette WebSocket → async message channel ---


class StarletteWebsocket:
    """Adapts a Starlette WebSocket to an async message channel."""

    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._ws.receive_bytes()
        except WebSocketDisconnect:
            raise StopAsyncIteration()

    async def send(self, message: bytes) -> None:
        try:
            await self._ws.send_bytes(message)
        except (WebSocketDisconnect, RuntimeError):
            # WebSocketDisconnect: first send after client disconnects.
            # RuntimeError: subsequent sends — Starlette's state machine
            # transitions to DISCONNECTED after the first failure, then
            # raises RuntimeError("Cannot call 'send' once a close message
            # has been sent") on any further send attempt.
            # Both are safe to suppress — the receive loop detects the
            # dead connection via StopAsyncIteration on the next read.
            pass


# --- Persistence: restore Y.Doc from SQLiteYStore ---


async def _restore_ydoc(store_cls: type[SQLiteYStore], path: str) -> Doc:
    """Create a Doc populated with any persisted state from the store."""
    doc = Doc()
    store = store_cls(path=path)
    async with store:
        try:
            await store.apply_updates(doc)
            log.info("Room %s: restored from store", path)
        except YDocNotFound:
            log.info("Room %s: created (no prior content)", path)
    return doc


# --- Room management using public pycrdt APIs ---


class Room:
    """A document room: shared Doc, connected channels, persistence.

    Each room runs a background task that listens for Doc changes via
    doc.events() and broadcasts updates to all connected channels while
    persisting to SQLiteYStore. Client connections are handled by serve().
    """

    def __init__(self, name: str, doc: Doc, store: SQLiteYStore):
        self.name = name
        self.doc = doc
        self.channels: set[StarletteWebsocket] = set()
        self._store = store
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the broadcast + persistence loop.

        Blocks until the doc observer is active and the store is ready,
        so serve() can safely be called immediately after start() returns.
        """
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(ready))
        await ready.wait()

    async def stop(self) -> None:
        """Stop the room and close the store."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self, ready: asyncio.Event) -> None:
        """Run the store and broadcast loop.

        The store must be running (async with) before we can call write().
        The doc.events() subscription must be active before any client can
        modify the doc, otherwise we'd miss events. The ready event signals
        both are established — including DB initialization completing.
        """
        async with self._store:
            await self._store.db_initialized.wait()
            async with self.doc.events() as events:
                ready.set()
                async for event in events:
                    update_msg = create_update_message(event.update)
                    # Broadcast concurrently — a slow client shouldn't
                    # block updates to everyone else in the room.
                    channels = list(self.channels)
                    if channels:
                        await asyncio.gather(*(self._safe_send(ch, update_msg) for ch in channels))
                    try:
                        await self._store.write(event.update)
                    except Exception:
                        log.warning(
                            "Failed to persist update for room %s",
                            self.name,
                            exc_info=True,
                        )

    async def _safe_send(self, channel: StarletteWebsocket, message: bytes) -> None:
        """Send to a channel, removing it on failure."""
        try:
            await channel.send(message)
        except Exception:
            self.channels.discard(channel)

    async def serve(self, channel: StarletteWebsocket) -> None:
        """Handle a single WebSocket client.

        Sends SYNC_STEP1 to initiate the handshake, then processes incoming
        sync messages. Doc changes from handle_sync_message trigger the
        broadcast loop (via doc.events()), which sends updates to all
        channels including this one. Yjs deduplicates on the client side.
        """
        self.channels.add(channel)
        log.info("Room %s: client connected (%d total)", self.name, len(self.channels))
        try:
            await channel.send(create_sync_message(self.doc))
            async for message in channel:
                if message[0] == YMessageType.SYNC:
                    reply = handle_sync_message(message[1:], self.doc)
                    if reply is not None:
                        await channel.send(reply)
        finally:
            self.channels.discard(channel)
            log.info("Room %s: client disconnected (%d remaining)", self.name, len(self.channels))


class RoomManager:
    """Manages document rooms with lazy creation and persistence restore."""

    def __init__(self, store_cls: type[SQLiteYStore], docs_dir: Path):
        self._store_cls = store_cls
        self._docs_dir = docs_dir
        self.rooms: dict[str, Room] = {}

    def _safe_doc_path(self, name: str) -> Path | None:
        """Resolve a room name to a .md path, rejecting traversal attempts."""
        md_path = (self._docs_dir / f"{name}.md").resolve()
        if not md_path.is_relative_to(self._docs_dir.resolve()):
            log.warning("Room name %r escapes docs_dir, rejected", name)
            return None
        return md_path

    async def get_room(self, name: str) -> Room:
        """Get or create a room, hydrating from .md file or SQLite.

        Priority: .md file in docs_dir → SQLite store → empty Doc.
        During migration, rooms without .md files fall back to SQLite.
        """
        if name not in self.rooms:
            md_path = self._safe_doc_path(name)
            if md_path is not None and md_path.exists():
                doc = Doc()
                text = doc.get("content", type=Text)
                content = md_path.read_text()
                if content:
                    with doc.transaction():
                        text += content
                log.info("Room %s: hydrated from %s", name, md_path)
            else:
                doc = await _restore_ydoc(self._store_cls, name)

            store = self._store_cls(path=name)
            room = Room(name, doc, store)
            await room.start()
            self.rooms[name] = room
        return self.rooms[name]

    async def remove_if_empty(self, name: str) -> None:
        """Stop and remove a room if no clients are connected.

        File-backed rooms are kept in memory to preserve CRDT state
        identity. Without this, WebSocket reconnections trigger re-hydration
        from file which creates new CRDT operations — these merge with the
        client's old state and duplicate content.
        """
        room = self.rooms.get(name)
        if room and not room.channels:
            md_path = self._safe_doc_path(name)
            if md_path is not None and md_path.exists():
                log.info("Room %s: last client left, keeping (file-backed)", name)
                return
            del self.rooms[name]
            await room.stop()
            log.info("Room %s: cleaned up (no clients)", name)

    async def close(self) -> None:
        """Stop all rooms."""
        for room in self.rooms.values():
            await room.stop()
        self.rooms.clear()


# --- App factory ---


def create_app(
    db_path: str | Path = ROOT / "data" / "tafelmusik.db",
    public_dir: str | Path = ROOT / "public",
    docs_dir: str | Path | None = None,
) -> Starlette:
    """Create the ASGI application with the given configuration."""

    _db_path = str(db_path)
    _docs_dir = Path(docs_dir) if docs_dir else Path(
        os.environ.get("TAFELMUSIK_DOCS_DIR", HOME / "Repos")
    )
    Store = type(
        "Store",
        (SQLiteYStore,),
        {
            "db_path": _db_path,
            # Squashing DISABLED — pycrdt-store 0.1.3 has a data-loss bug:
            # SQLiteYStore.write() lines 480-483 only call apply_update()
            # inside `if self._decompress:`, so without compression enabled
            # the squash replays into an empty Doc and destroys all data.
            # Re-enable after upstream fix.
            "squash_after_inactivity_of": None,
        },
    )

    manager = RoomManager(store_cls=Store, docs_dir=_docs_dir)

    async def websocket_endpoint(websocket: WebSocket):
        room_name = websocket.path_params.get("room", "default")
        await websocket.accept()
        channel = StarletteWebsocket(websocket)
        room = await manager.get_room(room_name)
        await room.serve(channel)
        await manager.remove_if_empty(room_name)

    def _query_persisted_rooms() -> set[str]:
        """Query SQLite for room names (runs in a thread to avoid blocking)."""
        try:
            with sqlite3.connect(_db_path) as conn:
                return {row[0] for row in conn.execute("SELECT DISTINCT path FROM yupdates")}
        except Exception:
            return set()  # DB may not exist yet

    def _scan_doc_files() -> set[str]:
        """Scan docs_dir for .md files, returning room names."""
        if not _docs_dir.exists():
            return set()
        return {
            str(md.relative_to(_docs_dir).with_suffix(""))
            for md in _docs_dir.rglob("*.md")
        }

    async def list_rooms(request):
        """Return room names from in-memory rooms, SQLite store, and .md files.

        Each room includes an ``active`` flag: true if the room is currently
        in memory with connected clients (excluding the MCP poller's own
        connections). The room poller uses this to avoid connecting to every
        file on disk.
        """
        memory_rooms = set(manager.rooms.keys())
        persisted_rooms = await asyncio.to_thread(_query_persisted_rooms)
        file_rooms = await asyncio.to_thread(_scan_doc_files)
        all_names = sorted(memory_rooms | persisted_rooms | file_rooms)
        rooms = [
            {"name": name, "active": name in memory_rooms}
            for name in all_names
        ]
        return JSONResponse({"rooms": rooms})

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await manager.close()

    _public_dir = str(public_dir)

    async def spa_fallback(request):
        """Serve index.html for any path not matched by API/WS/static routes."""
        return FileResponse(Path(_public_dir) / "index.html")

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/rooms", list_rooms),
            WebSocketRoute("/_ws/{room:path}", websocket_endpoint),
            Mount("/static", StaticFiles(directory=_public_dir)),
            Route("/{path:path}", spa_fallback),
            Route("/", spa_fallback),
        ],
    )
    app.state.manager = manager
    return app


# Default app for `uvicorn tafelmusik.asgi_server:app`
app = create_app()
