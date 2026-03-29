"""ASGI server entry point — always-running, holds Y.Doc, serves web editor."""

from contextlib import asynccontextmanager
from pathlib import Path

from pycrdt import Doc
from pycrdt.store import SQLiteYStore
from pycrdt.store.base import YDocNotFound
from pycrdt.websocket import WebsocketServer
from pycrdt.websocket.yroom import YRoom
from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute
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
        await self._ws.send_bytes(message)


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

    @asynccontextmanager
    async def lifespan(app):
        async with ws_server:
            yield

    return Starlette(
        lifespan=lifespan,
        routes=[
            WebSocketRoute("/{room:path}", websocket_endpoint),
            Mount("/", StaticFiles(directory=str(public_dir), html=True)),
        ],
    )


# Default app for `uvicorn tafelmusik.asgi_server:app`
app = create_app()
