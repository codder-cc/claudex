"""Live peer streaming over a LAN (or any peer link).

A host (machine B) runs `peer serve`: it advertises itself on the LAN via mDNS
and accepts authenticated, encrypted connections (claudex.core.peer). Each
connection is bridged to a live Claude streaming session on B, so a peer (machine
A) streams prompts straight into B's session and gets replies back — the remote
twin of mode C, but the control socket is an encrypted peer channel instead of a
local Unix socket.

mDNS (zeroconf) is optional: without it, discovery is disabled but a peer added
by `host:port` still connects.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Callable, List, Optional

from claudex.core.device import DeviceIdentity, load_device
from claudex.core.peer import PeerStore, SecureChannel, handshake

SERVICE_TYPE = "_claudex-peer._tcp.local."


# ── host (server) ─────────────────────────────────────────────────────────────
class PeerServer:
    def __init__(self, device: DeviceIdentity, store: PeerStore,
                 profile: str, model: Optional[str] = None, cwd: Optional[str] = None,
                 host: str = "0.0.0.0", port: int = 0,
                 on_event: Optional[Callable[[str], None]] = None):
        self.device = device
        self.store = store
        self.profile = profile
        self.model = model
        self.cwd = cwd
        self.host = host
        self.port = port
        self._on_event = on_event
        self._server: Optional[asyncio.AbstractServer] = None
        self._adv = None

    def _log(self, msg: str) -> None:
        if self._on_event:
            try:
                self._on_event(msg)
            except Exception:  # noqa: BLE001
                pass

    async def _run_session(self, ch) -> None:
        """Serve one peer: bridge the channel to a live session until it ends."""
        session = None
        try:
            while True:
                req = await ch.recv()
                if req is None:
                    break
                op = req.get("op")
                if op == "open":
                    session = await asyncio.to_thread(self._spawn_session, req.get("model"))
                    await ch.send({"ok": True})
                elif op == "ask":
                    if session is None:
                        session = await asyncio.to_thread(self._spawn_session, req.get("model"))
                    res = await asyncio.to_thread(session.ask, req.get("prompt", ""),
                                                  float(req.get("timeout", 600)))
                    await ch.send({"ok": True, "text": res.text, "is_error": res.is_error,
                                   "session_id": res.session_id, "cost_usd": res.cost_usd})
                    self._log(f"ran: {str(req.get('prompt',''))[:60]}")
                elif op == "interrupt":
                    if session:
                        await asyncio.to_thread(session.interrupt)
                    await ch.send({"ok": True})
                elif op == "close":
                    await ch.send({"ok": True})
                    break
                else:
                    await ch.send({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as e:  # noqa: BLE001
            self._log(f"peer session error: {e}")
        finally:
            if session:
                try:
                    await asyncio.to_thread(session.close)
                except Exception:
                    pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        addr = f"{peername[0]}:{peername[1]}" if peername else ""
        try:
            ch = await asyncio.wait_for(
                handshake(reader, writer, self.device, self.store,
                          initiator=False, addr=addr), timeout=15)
        except Exception as e:  # noqa: BLE001
            self._log(f"handshake failed from {addr}: {e}")
            try:
                writer.close()
            except Exception:
                pass
            return
        self._log(f"peer {ch.peer_name or ch.peer_device_id} connected from {addr}")
        try:
            await self._run_session(ch)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _spawn_session(self, model_override: Optional[str]):
        from claudex.core.auth import AuthManager
        from claudex.core.profile import ProfileManager
        from claudex.fleet.runner import build_claude_env
        from claudex.fleet.stream import StreamSession, build_stream_argv
        prof = ProfileManager().get(self.profile)
        env = build_claude_env(AuthManager(), self.profile, prof.config_dir)
        argv = build_stream_argv(model=model_override or self.model)
        return StreamSession(_spawn_proc(argv, env, self.cwd))

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._adv = advertise(self.device, self.port)
        return self.port

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._adv:
            close_advertisement(self._adv)
        if self._server:
            self._server.close()

    async def serve_relay(self, endpoint: str, room: str, token: str,
                          should_stop=None) -> None:
        """Host over a relay server: wait in <room> for a joiner, serve one
        session, then reconnect for the next. Works across NAT (no LAN needed)."""
        while not (should_stop and should_stop()):
            try:
                ws = await _relay_ws(endpoint, room, token)
            except Exception as e:  # noqa: BLE001
                self._log(f"relay connect failed: {e}")
                await asyncio.sleep(3)
                continue
            stream = WSStream(ws)
            try:
                ch = await handshake(stream, stream, self.device, self.store,
                                     initiator=False, addr=f"relay:{room}")
                self._log(f"peer {ch.peer_name or ch.peer_device_id} connected via relay")
                await self._run_session(ch)
            except Exception as e:  # noqa: BLE001
                self._log(f"relay session ended: {e}")
            finally:
                await stream.wait_closed()


def _spawn_proc(argv, env, cwd):
    import subprocess
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=cwd, text=True, bufsize=1)


# ── peer (client) ─────────────────────────────────────────────────────────────
class PeerClient:
    def __init__(self, ch: SecureChannel):
        self._ch = ch

    @classmethod
    async def connect(cls, host: str, port: int, device: DeviceIdentity,
                      store: PeerStore, expect_device_id: Optional[str] = None,
                      expect_pubkey: Optional[str] = None) -> "PeerClient":
        reader, writer = await asyncio.open_connection(host, port)
        ch = await handshake(reader, writer, device, store, initiator=True,
                             expect_device_id=expect_device_id,
                             expect_pubkey=expect_pubkey, addr=f"{host}:{port}")
        return cls(ch)

    @classmethod
    async def connect_relay(cls, endpoint: str, room: str, token: str,
                            device: DeviceIdentity, store: PeerStore,
                            expect_device_id: Optional[str] = None,
                            expect_pubkey: Optional[str] = None) -> "PeerClient":
        """Join <room> on a relay server and handshake with the host there."""
        ws = await _relay_ws(endpoint, room, token)
        stream = WSStream(ws)
        ch = await handshake(stream, stream, device, store, initiator=True,
                             expect_device_id=expect_device_id,
                             expect_pubkey=expect_pubkey, addr=f"relay:{room}")
        return cls(ch)

    @property
    def peer(self) -> str:
        return self._ch.peer_name or self._ch.peer_device_id

    async def ask(self, prompt: str, timeout: float = 600) -> dict:
        await self._ch.send({"op": "ask", "prompt": prompt, "timeout": timeout})
        return await self._ch.recv() or {"ok": False, "error": "no reply"}

    async def interrupt(self) -> None:
        await self._ch.send({"op": "interrupt"})
        await self._ch.recv()

    async def close(self) -> None:
        try:
            await self._ch.send({"op": "close"})
            await asyncio.wait_for(self._ch.recv(), timeout=2)
        except Exception:  # noqa: BLE001
            pass
        await self._ch.close()


# ── relay transport (WebSocket through a server) ──────────────────────────────
def _relay_url(endpoint: str, room: str) -> str:
    e = endpoint.rstrip("/")
    if e.startswith("https://"):
        base = "wss://" + e[len("https://"):]
    elif e.startswith("http://"):
        base = "ws://" + e[len("http://"):]
    else:
        base = e
    return f"{base}/ws/claudex/relay/{room}"


def _origin(endpoint: str) -> str:
    e = endpoint.rstrip("/")
    # scheme://host[:port] — the server's own origin, which its CORS allows
    for sep in ("://",):
        if sep in e:
            scheme, rest = e.split(sep, 1)
            return f"{scheme}://{rest.split('/', 1)[0]}"
    return e


async def _relay_ws(endpoint: str, room: str, token: str):
    import websockets
    url = _relay_url(endpoint, room) + f"?token={token}"
    # quart_cors rejects a WebSocket with no Origin; send the server's own origin
    # (an allowed one — codder.cc in prod, or a third-party relay's own host).
    return await websockets.connect(url, max_size=None, ping_interval=20,
                                    ping_timeout=20, origin=_origin(endpoint))


class WSStream:
    """Adapts a websockets connection to the reader/writer surface SecureChannel
    expects (readexactly / write+drain / close+wait_closed), so the peer channel
    runs unchanged over a relayed WebSocket. One write+drain == one WS message;
    readexactly reassembles across messages via an internal buffer."""

    def __init__(self, ws):
        self._ws = ws
        self._buf = b""
        self._pending = b""

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                msg = await self._ws.recv()
            except Exception:  # noqa: BLE001 — connection closed
                raise asyncio.IncompleteReadError(self._buf, n)
            if msg is None:
                raise asyncio.IncompleteReadError(self._buf, n)
            self._buf += msg if isinstance(msg, (bytes, bytearray)) else str(msg).encode()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def write(self, data: bytes) -> None:
        self._pending += data

    async def drain(self) -> None:
        if self._pending:
            data, self._pending = self._pending, b""
            await self._ws.send(data)

    def close(self) -> None:
        asyncio.ensure_future(self._ws.close())

    async def wait_closed(self) -> None:
        try:
            await self._ws.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    def get_extra_info(self, key, default=None):
        return default


# ── mDNS discovery (optional) ─────────────────────────────────────────────────
def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def advertise(device: DeviceIdentity, port: int):
    """Advertise this host on the LAN via mDNS. Returns a handle, or None."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except Exception:  # noqa: BLE001
        return None
    try:
        ip = _local_ip()
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{device.device_id}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={"device_id": device.device_id, "name": device.name,
                        "pubkey": device.public_key},
        )
        zc = Zeroconf()
        zc.register_service(info)
        return (zc, info)
    except Exception:  # noqa: BLE001
        return None


def close_advertisement(handle) -> None:
    if not handle:
        return
    zc, info = handle
    try:
        zc.unregister_service(info)
        zc.close()
    except Exception:  # noqa: BLE001
        pass


def discover(timeout: float = 2.0) -> List[dict]:
    """Browse the LAN for claudex peers. Empty list if zeroconf is unavailable."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:  # noqa: BLE001
        return []
    found: dict = {}

    class _Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if not info:
                return
            props = {k.decode(): (v.decode() if isinstance(v, bytes) else v)
                     for k, v in (info.properties or {}).items()}
            addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
            found[props.get("device_id", name)] = {
                "device_id": props.get("device_id"), "name": props.get("name"),
                "pubkey": props.get("pubkey"),
                "addr": addrs[0] if addrs else None, "port": info.port,
            }

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    import time
    try:
        zc = Zeroconf()
    except Exception:  # noqa: BLE001 — mDNS port unavailable; degrade to no discovery
        return []
    try:
        ServiceBrowser(zc, SERVICE_TYPE, _Listener())
        time.sleep(timeout)
    except Exception:  # noqa: BLE001
        return list(found.values())
    finally:
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass
    return list(found.values())
