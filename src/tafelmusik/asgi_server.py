"""ASGI server entry point — always-running, holds Y.Doc, serves web editor.

Owns its room management directly using public pycrdt APIs (Doc, sync
functions, SQLiteYStore). No dependency on pycrdt.websocket.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
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

from tafelmusik.anchored import Comment, CommentStore, anchor, capture_context
from tafelmusik.logging_config import configure_event_logging, configure_logging, log_event

PORT = 3456
ROOT = Path(__file__).resolve().parent.parent.parent
HOME = Path.home()

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
            log_event("room_restored", path)
        except YDocNotFound:
            log.info("Room %s: created (no prior content)", path)
            log_event("room_created", path)
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
            try:
                await asyncio.wait_for(self._store.db_initialized.wait(), timeout=5.0)
            except TimeoutError:
                log.warning("Room %s: db_initialized took >5s, still waiting", self.name)
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
            log.warning("Room %s: dropped channel on send failure", self.name)

    async def serve(self, channel: StarletteWebsocket) -> None:
        """Handle a single WebSocket client.

        Sends SYNC_STEP1 to initiate the handshake, then processes incoming
        sync messages. Doc changes from handle_sync_message trigger the
        broadcast loop (via doc.events()), which sends updates to all
        channels including this one. Yjs deduplicates on the client side.
        """
        self.channels.add(channel)
        log.info("Room %s: client connected (%d total)", self.name, len(self.channels))
        log_event("client_connected", self.name, clients=len(self.channels))
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
            log_event("client_disconnected", self.name, clients=len(self.channels))

    async def broadcast(self, message: bytes) -> None:
        """Send an arbitrary message to all connected channels."""
        channels = list(self.channels)
        if channels:
            await asyncio.gather(*(self._safe_send(ch, message) for ch in channels))


class RoomManager:
    """Manages document rooms with lazy creation and persistence restore."""

    def __init__(self, store_cls: type[SQLiteYStore], docs_dir: Path):
        self._store_cls = store_cls
        self._docs_dir = docs_dir
        self.rooms: dict[str, Room] = {}
        self.session_registry: dict[str, WebSocket] = {}

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
                log_event("room_hydrated", name, source=str(md_path), chars=len(content))
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
                log_event("room_retained", name, reason="file-backed")
                return
            del self.rooms[name]
            await room.stop()
            log.info("Room %s: cleaned up (no clients)", name)
            log_event("room_evicted", name)

    async def close(self) -> None:
        """Stop all rooms."""
        for room in self.rooms.values():
            await room.stop()
        self.rooms.clear()


# --- Comment events: WebSocket multiplexing ---

COMMENT_MSG_TYPE = b'\x01'


def _comment_dict(c: Comment, anchor_pos: dict | None = None) -> dict:
    d = {
        "id": c.id, "author": c.author, "created": c.created,
        "target": c.target, "body": c.body,
        "quote": c.quote, "prefix": c.prefix, "suffix": c.suffix,
        "replies_to": c.replies_to, "resolved": c.resolved,
    }
    if anchor_pos:
        d["anchor"] = anchor_pos
    return d


def _comment_event(event_type: str, comment: Comment) -> bytes:
    payload = json.dumps({"type": event_type, "comment": _comment_dict(comment)})
    return COMMENT_MSG_TYPE + payload.encode()


# --- App factory ---


def create_app(
    db_path: str | Path = ROOT / "data" / "tafelmusik.db",
    public_dir: str | Path = ROOT / "public",
    docs_dir: str | Path | None = None,
) -> Starlette:
    """Create the ASGI application with the given configuration."""
    configure_logging()
    event_log = configure_event_logging()
    if event_log:
        log.info("JSONL event log: %s", event_log)

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

    _comments_db = str(Path(db_path).parent / "comments.db")
    comment_store = CommentStore(_comments_db)

    async def websocket_endpoint(websocket: WebSocket):
        room_name = websocket.path_params.get("room", "default")
        await websocket.accept()
        channel = StarletteWebsocket(websocket)
        room = await manager.get_room(room_name)
        try:
            await room.serve(channel)
        finally:
            await manager.remove_if_empty(room_name)

    async def session_websocket(websocket: WebSocket):
        """Dedicated WebSocket for session-direct comment delivery.

        No CRDT sync, no room — just a persistent pipe registered in
        session_registry so session_comment() can push 0x01 events.
        """
        session_id = websocket.path_params["session_id"]
        await websocket.accept()
        manager.session_registry[session_id] = websocket
        log.info("Session %s: dedicated WebSocket connected", session_id)
        log_event("session_connected", session_id=session_id)
        try:
            # Hold connection open until client disconnects.
            # The only traffic is outbound 0x01 from session_comment().
            while True:
                await websocket.receive_bytes()
        except WebSocketDisconnect:
            pass
        finally:
            if manager.session_registry.get(session_id) is websocket:
                del manager.session_registry[session_id]
            log.info("Session %s: dedicated WebSocket disconnected", session_id)
            log_event("session_disconnected", session_id=session_id)

    def _query_persisted_rooms() -> set[str]:
        """Query SQLite for room names (runs in a thread to avoid blocking).

        Note: sqlite3's ``with conn:`` is a *transaction* manager (commit/rollback),
        NOT a resource manager — it does NOT close the connection. We must call
        conn.close() explicitly, otherwise every /api/rooms request leaks a
        file descriptor. This was the root cause of the server hanging after
        ~10 minutes (FD limit exhausted).
        """
        conn = None
        try:
            conn = sqlite3.connect(_db_path)
            return {row[0] for row in conn.execute("SELECT DISTINCT path FROM yupdates")}
        except Exception:
            log.warning("Failed to query persisted rooms from %s", _db_path, exc_info=True)
            return set()
        finally:
            if conn is not None:
                conn.close()

    # Directories skipped when scanning docs_dir for .md files.
    # Dotdirs (.bon, .claude, .git), dependency dirs, and build artifacts
    # contain markdown that isn't authored content.
    _SKIP_DIRS = frozenset({
        "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", "_build", ".tox", ".mypy_cache",
        ".hypothesis", "htmlcov", "site-packages",
    })

    # [timestamp, results] — mutable list avoids nonlocal for nested function
    _scan_cache = [0.0, set()]
    _SCAN_TTL = 30.0  # seconds

    def _scan_doc_files() -> set[str]:
        """Scan docs_dir for authored .md files, returning room names.

        Skips dotdirs (e.g. .git, .bon, .claude), dependency dirs, and
        build artifacts. These contain markdown but aren't documents
        you'd want to edit collaboratively.

        Results are cached for _SCAN_TTL seconds to avoid blocking the
        event loop with rglob on every /api/rooms request.
        """
        now = time.monotonic()
        if now - _scan_cache[0] < _SCAN_TTL:
            return _scan_cache[1]

        if not _docs_dir.exists():
            _scan_cache[0] = now
            _scan_cache[1] = set()
            return set()
        results = set()
        for md in _docs_dir.rglob("*.md"):
            rel = md.relative_to(_docs_dir)
            # Skip any path component starting with '.' or in the skip list
            if any(p.startswith(".") or p in _SKIP_DIRS for p in rel.parts[:-1]):
                continue
            results.add(str(rel.with_suffix("")))
        _scan_cache[0] = now
        _scan_cache[1] = results
        return results

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

    async def handle_comments(request):
        """GET: list comments for a room. POST: create a comment."""
        room_name = request.path_params["room"]
        if request.method == "GET":
            include_resolved = request.query_params.get("resolved", "false") == "true"
            comments = comment_store.list_for_target(
                room_name, include_resolved=include_resolved
            )

            anchors = {}
            if room_name in manager.rooms:
                text = manager.rooms[room_name].doc.get("content", type=Text)
                content = str(text)
                for c in comments:
                    if c.quote:
                        result = anchor(content, c.quote, c.prefix, c.suffix)
                        if result:
                            anchors[c.id] = {
                                "start": result.start,
                                "end": result.end,
                                "confident": result.confident,
                            }

            return JSONResponse([
                _comment_dict(c, anchor_pos=anchors.get(c.id))
                for c in comments
            ])

        # POST — create comment
        data = await request.json()
        for required in ("author", "body"):
            if required not in data:
                return JSONResponse(
                    {"error": f"missing required field: {required}"},
                    status_code=400,
                )
        quote = data.get("quote")
        prefix = data.get("prefix")
        suffix = data.get("suffix")

        # Compute anchor context from live document if not provided
        if quote and not prefix and room_name in manager.rooms:
            text = manager.rooms[room_name].doc.get("content", type=Text)
            content = str(text)
            result = anchor(content, quote)
            if result:
                prefix, suffix = capture_context(content, result.start, result.end)

        comment = comment_store.create(
            author=data["author"],
            target=room_name,
            body=data["body"],
            quote=quote,
            prefix=prefix,
            suffix=suffix,
            replies_to=data.get("replies_to"),
        )

        # Broadcast to room peers
        if room_name in manager.rooms:
            await manager.rooms[room_name].broadcast(
                _comment_event("comment_created", comment)
            )

        log_event("comment_created", room_name, author=data["author"], comment_id=comment.id)
        return JSONResponse(_comment_dict(comment), status_code=201)

    async def resolve_comment(request):
        """Mark a comment as resolved and broadcast to room peers."""
        comment_id = request.path_params["comment_id"]
        room_name = request.path_params["room"]
        comment = comment_store.resolve(comment_id)
        if not comment:
            return JSONResponse({"error": "not found"}, status_code=404)

        if room_name in manager.rooms:
            await manager.rooms[room_name].broadcast(
                _comment_event("comment_resolved", comment)
            )

        log_event("comment_resolved", room_name, comment_id=comment_id)
        return JSONResponse(_comment_dict(comment))

    async def session_comment(request):
        """Post a comment to a specific Claude session, not a room."""
        session_id = request.path_params["session_id"]
        ws = manager.session_registry.get(session_id)
        if ws is None:
            return JSONResponse(
                {"error": "session not connected"}, status_code=404,
            )

        data = await request.json()
        for required in ("author", "body"):
            if required not in data:
                return JSONResponse(
                    {"error": f"missing required field: {required}"},
                    status_code=400,
                )

        comment = comment_store.create(
            author=data["author"],
            target=f"session:{session_id}",
            body=data["body"],
            quote=data.get("quote"),
            prefix=data.get("prefix"),
            suffix=data.get("suffix"),
            replies_to=data.get("replies_to"),
        )

        event = _comment_event("comment_created", comment)
        try:
            await ws.send_bytes(event)
        except (WebSocketDisconnect, RuntimeError):
            manager.session_registry.pop(session_id, None)
            log.warning("Session %s: send failed, removed from registry", session_id)

        log_event("session_comment", session_id, author=data["author"], comment_id=comment.id)
        return JSONResponse(_comment_dict(comment), status_code=201)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            comment_store.close()
            await manager.close()

    _public_dir = str(public_dir)

    async def spa_fallback(request):
        """Serve index.html for any path not matched by API/WS/static routes."""
        return FileResponse(Path(_public_dir) / "index.html")

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/sessions/{session_id}/comments", session_comment, methods=["POST"]),
            Route("/api/rooms", list_rooms),
            Route(
                "/api/rooms/{room:path}/comments/{comment_id}/resolve",
                resolve_comment, methods=["POST"],
            ),
            Route("/api/rooms/{room:path}/comments", handle_comments, methods=["GET", "POST"]),
            WebSocketRoute("/_ws/_session/{session_id}", session_websocket),
            WebSocketRoute("/_ws/{room:path}", websocket_endpoint),
            Mount("/static", StaticFiles(directory=_public_dir)),
            Route("/{path:path}", spa_fallback),
            Route("/", spa_fallback),
        ],
    )
    app.state.manager = manager
    app.state.comment_store = comment_store
    return app


# Default app for `uvicorn tafelmusik.asgi_server:app`
app = create_app()
