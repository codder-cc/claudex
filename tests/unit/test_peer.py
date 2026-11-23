"""Tests for the authenticated encrypted peer channel + TOFU store."""

import asyncio

import pytest

from claudex import crypto
from claudex.core.device import DeviceIdentity
from claudex.core.peer import PeerError, PeerStore, handshake


def _device(name: str) -> DeviceIdentity:
    priv, pub = crypto.generate_keypair()
    return DeviceIdentity(device_id=pub[:12], name=name, public_key=pub, private_key=priv)


# ── TOFU store ────────────────────────────────────────────────────────────────
def test_tofu_pins_then_verifies(tmp_path):
    store = PeerStore(tmp_path / "peers.json")
    store.verify_or_pin("devA", "key1", name="A")      # first contact: pin
    store.verify_or_pin("devA", "key1")                # same key: ok
    assert store.get("devA")["public_key"] == "key1"
    with pytest.raises(PeerError):
        store.verify_or_pin("devA", "key2")            # changed key: abort
    assert store.forget("devA") is True
    store.verify_or_pin("devA", "key2")                # re-pin after forget


# ── channel handshake over loopback ───────────────────────────────────────────
async def _run_pair(server_dev, client_dev, server_store, client_store,
                    client_expect_pub=None):
    result = {}

    async def on_conn(reader, writer):
        try:
            ch = await handshake(reader, writer, server_dev, server_store, initiator=False)
            msg = await ch.recv()
            await ch.send({"echo": msg["hi"], "from": "server"})
            result["server_saw_peer"] = ch.peer_device_id
        except Exception as e:  # noqa: BLE001
            result["server_err"] = repr(e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        ch = await asyncio.wait_for(
            handshake(reader, writer, client_dev, client_store, initiator=True,
                      expect_pubkey=client_expect_pub), timeout=5)
        await ch.send({"hi": "hello-peer"})
        result["client_reply"] = await asyncio.wait_for(ch.recv(), timeout=5)
        result["client_saw_peer"] = ch.peer_device_id
    finally:
        try:
            writer.close()
        except Exception:
            pass
        server.close()
    return result


async def test_handshake_and_encrypted_roundtrip(tmp_path):
    server_dev, client_dev = _device("srv"), _device("cli")
    ss, cs = PeerStore(tmp_path / "s.json"), PeerStore(tmp_path / "c.json")
    res = await _run_pair(server_dev, client_dev, ss, cs,
                          client_expect_pub=server_dev.public_key)
    assert res["client_reply"]["echo"] == "hello-peer"
    assert res["client_reply"]["from"] == "server"
    assert res["client_saw_peer"] == server_dev.device_id
    assert res["server_saw_peer"] == client_dev.device_id
    # both sides pinned each other
    assert ss.get(client_dev.device_id)["public_key"] == client_dev.public_key
    assert cs.get(server_dev.device_id)["public_key"] == server_dev.public_key


async def test_handshake_rejects_wrong_expected_key(tmp_path):
    server_dev, client_dev = _device("srv"), _device("cli")
    ss, cs = PeerStore(tmp_path / "s.json"), PeerStore(tmp_path / "c.json")
    wrong = _device("other").public_key
    with pytest.raises(PeerError):
        await _run_pair(server_dev, client_dev, ss, cs, client_expect_pub=wrong)


async def test_handshake_aborts_on_pinned_key_change(tmp_path):
    server_dev, client_dev = _device("srv"), _device("cli")
    ss, cs = PeerStore(tmp_path / "s.json"), PeerStore(tmp_path / "c.json")
    # client has a DIFFERENT key pinned for this server device id
    cs.pin(server_dev.device_id, _device("imposter").public_key)
    with pytest.raises(PeerError):
        await _run_pair(server_dev, client_dev, ss, cs)
