"""ASGI server entry point — always-running, holds Y.Doc, serves web editor."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from pycrdt import Doc
from pycrdt.store import SQLiteYStore
from pycrdt.store.base import YDocNotFound
from pycrdt.websocket import WebsocketServer
from pycrdt.websocket.yroom import YRoom
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

PORT = 3456
ROOT = Path(__file__).resolve().parent.parent.parent


# --- Channel adapter: Starlette WebSocket → pycrdt Channel interface ---


class StarletteWebsocket:
    """Adapts a Starlette WebSocket to the pycrdt Channel interface."""

    def __init__(self, websocket: WebSocket, path: str):
        self._ws = websocket
        self._path = path

    @property
    def path(self) -> str:
        return self._path

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
        except YDocNotFound:
            pass  # Fresh room, no prior content
    return doc


# --- WebsocketServer with SQLite persistence ---


class TafelmusikWebsocketServer(WebsocketServer):
    """WebsocketServer that creates rooms with SQLiteYStore persistence.

    Follows the make_ydoc pattern from pycrdt-websocket's Django consumer:
    pre-populate a Doc from the store, then pass both the Doc and a fresh
    store instance to YRoom.
    """

    def __init__(self, store_cls: type[SQLiteYStore], **kwargs):
        super().__init__(**kwargs)
        self._store_cls = store_cls

    async def get_room(self, name: str) -> YRoom:
        if name not in self.rooms:
            doc = await _restore_ydoc(self._store_cls, name)
            store = self._store_cls(path=name)
            self.rooms[name] = YRoom(ydoc=doc, ystore=store, log=self.log)
        room = self.rooms[name]
        await self.start_room(room)
        return room


# --- App factory ---


def create_app(
    db_path: str | Path = ROOT / "data" / "tafelmusik.db",
    public_dir: str | Path = ROOT / "public",
) -> Starlette:
    """Create the ASGI application with the given configuration."""

    _db_path = str(db_path)
    Store = type("Store", (SQLiteYStore,), {
        "db_path": _db_path,
        "squash_after_inactivity_of": 60,  # compact updates after 60s of no edits
    })

    ws_server = TafelmusikWebsocketServer(store_cls=Store)

    async def websocket_endpoint(websocket: WebSocket):
        room = websocket.path_params.get("room", "default")
        await websocket.accept()
        channel = StarletteWebsocket(websocket, room)
        await ws_server.serve(channel)

    def _query_persisted_rooms() -> set[str]:
        """Query SQLite for room names (runs in a thread to avoid blocking)."""
        try:
            conn = sqlite3.connect(_db_path)
            rooms = {row[0] for row in conn.execute("SELECT DISTINCT path FROM yupdates")}
            conn.close()
            return rooms
        except Exception:
            return set()  # DB may not exist yet

    async def list_rooms(request):
        """Return room names from both in-memory rooms and SQLite store."""
        memory_rooms = set(ws_server.rooms.keys())
        persisted_rooms = await asyncio.to_thread(_query_persisted_rooms)
        all_rooms = sorted(memory_rooms | persisted_rooms)
        return JSONResponse({"rooms": all_rooms})

    @asynccontextmanager
    async def lifespan(app):
        async with ws_server:
            yield

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/rooms", list_rooms),
            WebSocketRoute("/{room:path}", websocket_endpoint),
            Mount("/", StaticFiles(directory=str(public_dir), html=True)),
        ],
    )


# Default app for `uvicorn tafelmusik.asgi_server:app`
app = create_app()
