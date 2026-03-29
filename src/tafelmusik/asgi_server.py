"""ASGI server entry point — always-running, holds Y.Doc, serves web editor."""

from contextlib import asynccontextmanager
from pathlib import Path

from pycrdt.store import SQLiteYStore
from pycrdt.store.base import YDocNotFound
from pycrdt.websocket import ASGIServer, WebsocketServer
from pycrdt.websocket.yroom import YRoom
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

PORT = 3456
ROOT = Path(__file__).resolve().parent.parent.parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"


class TafelmusikStore(SQLiteYStore):
    db_path = str(DATA_DIR / "tafelmusik.db")


class TafelmusikWebsocketServer(WebsocketServer):
    """WebsocketServer subclass that creates rooms with SQLiteYStore persistence.

    Rooms start with ready=False. Once the store is initialized (by YRoom internals),
    we restore persisted state via store.apply_updates(), then set ready=True so
    clients can sync.
    """

    async def get_room(self, name: str) -> YRoom:
        if name not in self.rooms:
            store = TafelmusikStore(path=name)
            room = YRoom(
                ready=False,
                ystore=store,
                log=self.log,
            )
            self.rooms[name] = room
            await self.start_room(room)
            # Wait for YRoom's _broadcast_updates to start the store
            await store.started.wait()
            await store.db_initialized.wait()
            # Restore persisted state (doc.observe not yet active since ready=False)
            try:
                await store.apply_updates(room.ydoc)
            except YDocNotFound:
                pass  # Fresh room, no prior content
            room.ready = True
        else:
            room = self.rooms[name]
            await self.start_room(room)
        return room


websocket_server = TafelmusikWebsocketServer()
asgi_ws = ASGIServer(websocket_server)


@asynccontextmanager
async def lifespan(app):
    async with websocket_server:
        yield


http_app = Starlette(
    lifespan=lifespan,
    routes=[
        Mount("/", StaticFiles(directory=PUBLIC_DIR, html=True)),
    ],
)


async def app(scope, receive, send):
    """ASGI dispatcher — websocket to pycrdt, everything else to starlette."""
    if scope["type"] == "websocket":
        await asgi_ws(scope, receive, send)
    else:
        await http_app(scope, receive, send)
