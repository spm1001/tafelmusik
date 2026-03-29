"""Validation tests for pycrdt provenance capabilities.

These tests verify three mechanisms needed for authorship tracking:
1. Transaction origins visible in-process via text.observe()
2. Transaction origins visible cross-wire (after sync through server)
3. Formatting attributes survive sync (for iA Writer-style authorship)
"""

import asyncio

from pycrdt import Doc, Text

from tafelmusik.conftest import connect_peer, get_free_port

# --- Test 1: Transaction origins visible in-process ---


def test_origin_visible_in_observe_callback():
    """text.observe() callback receives txn.origin when set via doc.transaction()."""
    doc = Doc()
    doc["content"] = text = Text()

    observed_origins = []

    def on_change(event, txn):
        observed_origins.append(txn.origin)

    text.observe(on_change)

    # Write with explicit origin
    with doc.transaction(origin="claude"):
        text += "Hello from Claude"

    # Write without origin
    text += " and anonymous"

    assert len(observed_origins) == 2
    assert observed_origins[0] == "claude"
    assert observed_origins[1] is None


# --- Test 2: Transaction origins after applying remote update ---


def test_origin_not_visible_after_remote_update():
    """Origins set on the sender's Doc are NOT visible on the receiver's Doc.

    This confirms that origins are local to a Doc instance and don't
    survive binary encoding. If this test FAILS (origin IS visible),
    then a separate channel server process could distinguish edit sources.
    """
    sender = Doc()
    sender["content"] = sender_text = Text()

    receiver = Doc()
    receiver["content"] = receiver_text = Text()

    observed_origins = []

    def on_change(event, txn):
        observed_origins.append(txn.origin)

    receiver_text.observe(on_change)

    # Sender writes with origin — then we apply the update to receiver
    with sender.transaction(origin="claude"):
        sender_text += "Hello from Claude"

    # Get the binary update from sender and apply to receiver
    update = sender.get_update()
    receiver.apply_update(update)

    # The receiver's callback should see origin=None (not "claude")
    assert len(observed_origins) == 1
    assert observed_origins[0] is None, (
        f"Origin survived the wire! Got {observed_origins[0]!r} — "
        "this means separate channel_server.py CAN use origins"
    )


# --- Test 3: Formatting attributes survive sync ---


def test_attrs_survive_local_roundtrip():
    """Formatting attributes written via insert() are readable via diff()."""
    doc = Doc()
    doc["content"] = text = Text()

    text.insert(0, "Claude wrote this", attrs={"author": "claude"})
    text.insert(len(str(text)), "\nSameer wrote this", attrs={"author": "sameer"})

    chunks = text.diff()
    assert len(chunks) == 2
    assert chunks[0] == ("Claude wrote this", {"author": "claude"})
    assert chunks[1] == ("\nSameer wrote this", {"author": "sameer"})


def test_attrs_survive_binary_update():
    """Formatting attributes survive encoding into a binary update and back."""
    sender = Doc()
    sender["content"] = sender_text = Text()

    sender_text.insert(0, "Claude wrote this", attrs={"author": "claude"})
    sender_text.insert(len(str(sender_text)), "\nSameer wrote this", attrs={"author": "sameer"})

    # Apply sender's full state to a fresh receiver
    receiver = Doc()
    receiver["content"] = receiver_text = Text()
    receiver.apply_update(sender.get_update())

    chunks = receiver_text.diff()
    assert len(chunks) == 2
    assert chunks[0] == ("Claude wrote this", {"author": "claude"})
    assert chunks[1] == ("\nSameer wrote this", {"author": "sameer"})


async def test_attrs_survive_server_sync(tmp_path):
    """Formatting attributes survive a full round-trip through the ASGI server."""
    import uvicorn

    from tafelmusik.asgi_server import create_app

    app = create_app(db_path=tmp_path / "attrs.db", public_dir=tmp_path)
    (tmp_path / "index.html").write_text("<html></html>")
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    await asyncio.sleep(1)

    try:
        # Client 1 writes with attrs
        async with connect_peer(port, "attrs-test") as t1:
            t1.insert(0, "Claude section", attrs={"author": "claude"})
            t1.insert(len(str(t1)), "\nHuman section", attrs={"author": "sameer"})
            await asyncio.sleep(0.5)

        # Client 2 reads — attrs should survive sync + persistence
        async with connect_peer(port, "attrs-test") as t2:
            chunks = t2.diff()
            # Check that attrs survived the round-trip
            authors = {attrs.get("author") for _, attrs in chunks if attrs}
            assert "claude" in authors, f"Claude attr lost. Chunks: {chunks}"
            assert "sameer" in authors, f"Sameer attr lost. Chunks: {chunks}"
    finally:
        srv.should_exit = True
        await asyncio.sleep(0.5)
        task.cancel()
