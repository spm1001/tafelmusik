# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pycrdt>=0.12.50",
#     "pycrdt-websocket>=0.16.0",
#     "httpx-ws>=0.7.0",
#     "uvicorn>=0.34.0",
# ]
# ///
"""Full spike: server + client in one process using threading for uvicorn."""
import asyncio
import threading
import time

PORT = 13461

def run_server():
    """Run uvicorn in a separate thread (it wants its own event loop)."""
    import uvicorn
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def _serve():
        from pycrdt.websocket import ASGIServer, WebsocketServer
        ws_server = WebsocketServer()
        await ws_server.start()
        app = ASGIServer(ws_server)
        config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
        server = uvicorn.Server(config)
        await server.serve()
    
    loop.run_until_complete(_serve())

async def run_client():
    from pycrdt import Doc, Text
    from pycrdt.websocket.yroom import Provider
    from pycrdt.websocket.websocket import HttpxWebsocket
    import httpx
    from httpx_ws import aconnect_ws

    ROOM = "spike-room"
    
    doc = Doc()
    doc["content"] = text = Text()

    print("Connecting client to server...")
    async with httpx.AsyncClient() as client:
        async with aconnect_ws(f"http://127.0.0.1:{PORT}/{ROOM}", client) as ws:
            channel = HttpxWebsocket(ws, ROOM)
            provider = Provider(doc, channel)
            task = asyncio.create_task(provider.start())
            await asyncio.sleep(1)

            # Write
            text += "Hello from pycrdt client!"
            await asyncio.sleep(0.5)
            print(f"Client1 wrote: {str(text)!r}")

            # Second client to verify sync through server
            doc2 = Doc()
            doc2["content"] = text2 = Text()
            async with aconnect_ws(f"http://127.0.0.1:{PORT}/{ROOM}", client) as ws2:
                ch2 = HttpxWebsocket(ws2, ROOM)
                p2 = Provider(doc2, ch2)
                t2 = asyncio.create_task(p2.start())
                await asyncio.sleep(1)

                val2 = str(text2)
                print(f"Client2 sees:  {val2!r}")
                
                if val2 == "Hello from pycrdt client!":
                    print("✅ Client→Server→Client sync WORKS")
                else:
                    print(f"⚠️  Expected 'Hello from pycrdt client!', got {val2!r}")

                # Bidirectional: client2 writes
                text2 += "\nEdited by client2!"
                await asyncio.sleep(0.5)
                val1 = str(text)
                print(f"Client1 sees:  {val1!r}")
                
                if "Edited by client2!" in val1:
                    print("✅ Bidirectional sync WORKS")
                else:
                    print(f"⚠️  Bidirectional sync failed")

                await p2.stop()
            await provider.stop()
    
    print("\n=== SPIKE COMPLETE ===")

# Start server in daemon thread
print(f"Starting server on port {PORT}...")
server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()
time.sleep(2)  # Wait for server to be ready
print("Server ready.")

# Run client
asyncio.run(run_client())
