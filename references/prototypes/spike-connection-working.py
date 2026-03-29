# /// script
# requires-python = ">=3.11"
# dependencies = ["pycrdt>=0.12.50", "pycrdt-websocket>=0.16.0", "uvicorn>=0.34.0", "httpx-ws>=0.7.0"]
# ///
"""Debug: try ws_server.start() as a task, not awaited directly."""
import asyncio

async def main():
    from pycrdt.websocket import ASGIServer, WebsocketServer
    from pycrdt.websocket.yroom import Provider
    from pycrdt.websocket.websocket import HttpxWebsocket
    import httpx
    from httpx_ws import aconnect_ws
    import uvicorn

    PORT = 13463
    ROOM = "spike"

    ws_server = WebsocketServer()
    # Start as concurrent task — it runs forever
    ws_task = asyncio.create_task(ws_server.start())
    await asyncio.sleep(0.1)
    print(f"WebsocketServer started (task)", flush=True)

    app = ASGIServer(ws_server)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1)
    print(f"Uvicorn listening on {PORT}", flush=True)

    # Try client connection
    from pycrdt import Doc, Text
    doc = Doc()
    doc["content"] = text = Text()

    try:
        async with httpx.AsyncClient() as client:
            async with aconnect_ws(f"http://127.0.0.1:{PORT}/{ROOM}", client) as ws:
                channel = HttpxWebsocket(ws, ROOM)
                provider = Provider(doc, channel)
                ptask = asyncio.create_task(provider.start())
                await asyncio.sleep(1)

                text += "Hello from spike!"
                await asyncio.sleep(0.5)
                print(f"Client wrote: {str(text)!r}", flush=True)

                # Second client to verify
                doc2 = Doc()
                doc2["content"] = t2 = Text()
                async with aconnect_ws(f"http://127.0.0.1:{PORT}/{ROOM}", client) as ws2:
                    ch2 = HttpxWebsocket(ws2, ROOM)
                    p2 = Provider(doc2, ch2)
                    t2task = asyncio.create_task(p2.start())
                    await asyncio.sleep(1)

                    print(f"Client2 sees: {str(t2)!r}", flush=True)
                    if str(t2) == "Hello from spike!":
                        print("✅ SYNC WORKS", flush=True)
                    else:
                        print("⚠️  Sync mismatch", flush=True)

                    t2 += "\nFrom client2!"
                    await asyncio.sleep(0.5)
                    if "From client2!" in str(text):
                        print("✅ BIDIRECTIONAL WORKS", flush=True)
                    else:
                        print(f"⚠️  Bidirectional: client1 sees {str(text)!r}", flush=True)

                    await p2.stop()
                await provider.stop()
    except Exception as e:
        import traceback
        print(f"ERROR: {e}", flush=True)
        traceback.print_exc()

    server.should_exit = True
    ws_task.cancel()
    await asyncio.sleep(0.5)
    print("\n=== SPIKE COMPLETE ===", flush=True)

asyncio.run(main())
