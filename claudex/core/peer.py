"""Peer links — an authenticated, encrypted channel between two machines.

Transport-agnostic: the handshake and framing run over any asyncio
reader/writer pair, whether that is a direct LAN socket or a relayed stream
through a server. Security model (trust-on-first-use):

  * Each machine has a long-lived X25519 *device* key (claudex.core.device).
  * A fresh ephemeral X25519 pair per connection gives the session key
    (forward secrecy): K = HKDF(ECDH(eph_a, eph_b)).
  * Each side proves possession of its device key by unsealing a challenge
    sealed to its device public key, then MACing it under K. The first time we
    see a peer we pin its device key; later connections must present the same
    one (a changed key aborts the handshake).

After the handshake, frames are length-prefixed AES-256-GCM, one counter per
direction. Payloads are JSON (with optional base64 binary fields).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from claudex import crypto
from claudex.constants import CLAUDEX_HOME
from claudex.core.device import DeviceIdentity

PEERS_FILE = CLAUDEX_HOME / "peers.json"
_PEER_INFO = b"claudex-peer-v1"
_MAX_FRAME = 32 * 1024 * 1024


class PeerError(Exception):
    pass


# ── TOFU peer store ───────────────────────────────────────────────────────────
class PeerStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or PEERS_FILE

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def list(self) -> list:
        return [{"device_id": k, **v} for k, v in self._load().items()]

    def get(self, device_id: str) -> Optional[dict]:
        return self._load().get(device_id)

    def pin(self, device_id: str, public_key: str, name: str = "",
            addr: str = "") -> None:
        data = self._load()
        entry = data.get(device_id, {})
        entry.update({"public_key": public_key})
        if name:
            entry["name"] = name
        if addr:
            entry["addr"] = addr
        data[device_id] = entry
        self._save(data)

    def forget(self, device_id: str) -> bool:
        data = self._load()
        if device_id in data:
            del data[device_id]
            self._save(data)
            return True
        return False

    def verify_or_pin(self, device_id: str, public_key: str, name: str = "",
                      addr: str = "") -> None:
        """TOFU: pin an unknown peer; a known peer must present the same key."""
        existing = self.get(device_id)
        if existing and existing.get("public_key") != public_key:
            raise PeerError(
                f"device {device_id} presented a DIFFERENT key than pinned "
                f"(possible impersonation) — `peer forget {device_id}` to re-pin")
        self.pin(device_id, public_key, name=name, addr=addr)


# ── channel crypto ────────────────────────────────────────────────────────────
def _session_key(eph_priv: X25519PrivateKey, peer_eph_pub_raw: bytes) -> bytes:
    shared = eph_priv.exchange(X25519PublicKey.from_public_bytes(peer_eph_pub_raw))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_PEER_INFO).derive(shared)


def _b64e(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    import base64
    return base64.b64decode(text)


@dataclass
class SecureChannel:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    key: bytes
    peer_device_id: str
    peer_name: str
    _send_ctr: int = 0
    _recv_ctr: int = 0
    _send_prefix: int = 0   # 0 for the initiator, 1 for the responder
    _recv_prefix: int = 1

    def _nonce(self, prefix: int, ctr: int) -> bytes:
        return struct.pack(">I", prefix) + struct.pack(">Q", ctr)

    async def send(self, obj: dict) -> None:
        aes = AESGCM(self.key)
        nonce = self._nonce(self._send_prefix, self._send_ctr)
        self._send_ctr += 1
        ct = aes.encrypt(nonce, json.dumps(obj).encode("utf-8"), None)
        self.writer.write(struct.pack(">I", len(ct)) + ct)
        await self.writer.drain()

    async def recv(self) -> Optional[dict]:
        try:
            header = await self.reader.readexactly(4)
        except asyncio.IncompleteReadError:
            return None
        (length,) = struct.unpack(">I", header)
        if length > _MAX_FRAME:
            raise PeerError("frame too large")
        ct = await self.reader.readexactly(length)
        aes = AESGCM(self.key)
        nonce = self._nonce(self._recv_prefix, self._recv_ctr)
        self._recv_ctr += 1
        try:
            pt = aes.decrypt(nonce, ct, None)
        except Exception as e:  # noqa: BLE001
            raise PeerError(f"decryption failed: {e}")
        return json.loads(pt.decode("utf-8"))

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length > _MAX_FRAME:
        raise PeerError("handshake frame too large")
    raw = await reader.readexactly(length)
    return json.loads(raw.decode("utf-8"))


async def _write_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    raw = json.dumps(obj).encode("utf-8")
    writer.write(struct.pack(">I", len(raw)) + raw)
    await writer.drain()


async def _challenge(reader, writer, key, peer_device_pub) -> None:
    """Send a sealed nonce and verify the peer's MAC proof under K."""
    my_nonce = os.urandom(32)
    await _write_frame(writer, {"t": "chal", "sealed": _b64e(crypto.seal(peer_device_pub, my_nonce))})
    msg = await _read_frame(reader)
    if msg.get("t") != "proof":
        raise PeerError("expected proof")
    expected = hmac.new(key, my_nonce, hashlib.sha256).digest()
    if not hmac.compare_digest(_b64d(msg["proof"]), expected):
        raise PeerError("peer failed to prove its device key")


async def _answer(reader, writer, key, device) -> None:
    """Unseal the peer's challenge and return the MAC proof under K."""
    msg = await _read_frame(reader)
    if msg.get("t") != "chal":
        raise PeerError("expected challenge")
    nonce = crypto.unseal(device.private_key, _b64d(msg["sealed"]))
    proof = hmac.new(key, nonce, hashlib.sha256).digest()
    await _write_frame(writer, {"t": "proof", "proof": _b64e(proof)})


async def _mutual_auth(reader, writer, key: bytes, device: DeviceIdentity,
                       peer_device_pub: str, initiator: bool) -> None:
    """Two sequential rounds (no interleaving): initiator challenges first, then
    the responder challenges. Each side proves possession of its device key by
    unsealing a nonce sealed to that key and MACing it under the session key K."""
    if initiator:
        await _challenge(reader, writer, key, peer_device_pub)   # round 1
        await _answer(reader, writer, key, device)               # round 2
    else:
        await _answer(reader, writer, key, device)               # round 1
        await _challenge(reader, writer, key, peer_device_pub)   # round 2


async def handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    device: DeviceIdentity, store: PeerStore, *, initiator: bool,
                    expect_device_id: Optional[str] = None,
                    expect_pubkey: Optional[str] = None,
                    addr: str = "") -> SecureChannel:
    """Run the authenticated handshake and return an encrypted channel.

    *expect_pubkey* (from mDNS/`peer add`) lets the initiator seal its challenge
    to the right device key even on the very first contact.
    """
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()

    hello = {"t": "hello", "device_id": device.device_id, "name": device.name,
             "device_pub": device.public_key, "eph_pub": _b64e(eph_pub)}
    if initiator:
        await _write_frame(writer, hello)
        peer_hello = await _read_frame(reader)
    else:
        peer_hello = await _read_frame(reader)
        await _write_frame(writer, hello)

    if peer_hello.get("t") != "hello":
        raise PeerError("bad hello")
    peer_device_id = peer_hello["device_id"]
    peer_device_pub = peer_hello["device_pub"]
    peer_eph_pub = _b64d(peer_hello["eph_pub"])

    if expect_device_id and peer_device_id != expect_device_id:
        raise PeerError(f"connected to {peer_device_id}, expected {expect_device_id}")
    if expect_pubkey and peer_device_pub != expect_pubkey:
        raise PeerError("peer device key does not match the expected one")

    # TOFU: pin or verify the peer's device key.
    store.verify_or_pin(peer_device_id, peer_device_pub,
                        name=peer_hello.get("name", ""), addr=addr)

    key = _session_key(eph, peer_eph_pub)
    await _mutual_auth(reader, writer, key, device, peer_device_pub, initiator)

    ch = SecureChannel(reader=reader, writer=writer, key=key,
                       peer_device_id=peer_device_id,
                       peer_name=peer_hello.get("name", ""))
    if not initiator:
        ch._send_prefix, ch._recv_prefix = 1, 0
    return ch
