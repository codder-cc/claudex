"""Local reverse proxy to the real Anthropic API (asyncio, keep-alive).

Why this exists
---------------
On Claude Code 2.1.x an org-pushed managed policy is gateway-verified and
applied from the in-memory fetch result, so no local file/env/shim trick can
neutralise it in a normal session. The one lever that works (for a *fail-open*
org) is making Claude Code not reach the first-party settings gateway: when
``ANTHROPIC_BASE_URL`` is not an Anthropic host, Claude Code skips the
managed-settings pull while inference still needs the real API.

This module runs a tiny reverse proxy on ``127.0.0.1`` that transparently
forwards every request to ``api.anthropic.com``. Point ``ANTHROPIC_BASE_URL``
at it → the org policy is skipped and inference still works.

Implementation: a single-threaded asyncio event loop handles any number of
concurrent streams (incl. SSE) without a thread-per-request, and upstream TLS
connections are pooled (keep-alive) so each request skips a fresh handshake.
Response framing (Content-Length / chunked) is forwarded verbatim. Stdlib only.

NOTE: only usable where the org policy is fail-open. A fail-closed org blocks
startup when the gateway is unreachable and cannot be overridden locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME, IS_WINDOWS

UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443

#: Liveness file for the shared proxy daemon (one proxy serves every launch).
PROXY_FILE = CLAUDEX_HOME / ".proxy"

# Hop-by-hop headers to drop when forwarding. Framing headers (content-length,
# transfer-encoding) are intentionally kept so both ends frame the body.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "upgrade",
}

_MAX_POOL = 32          # pooled upstream connections per proxy
_READ_LIMIT = 1 << 20   # 1 MiB StreamReader buffer (headers / chunk lines)


def proxy_env(base_url: str) -> dict:
    """Env applied for a proxy-mode launch: route through the local proxy AND
    turn telemetry off by default (org OTEL is already skipped with the managed
    pull; this also disables Claude Code's own first-party analytics)."""
    return {"ANTHROPIC_BASE_URL": base_url, "DISABLE_TELEMETRY": "1"}


# ---------------------------------------------------------------------------
# Async reverse-proxy server
# ---------------------------------------------------------------------------

def _header_lines(block: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ln in block.split("\r\n")[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            out.append((k.strip(), v.strip()))
    return out


def _lower(headers: list[tuple[str, str]]) -> dict:
    return {k.lower(): v for k, v in headers}


class AsyncProxyServer:
    """Reverse proxy to ``upstream_host`` on its own asyncio loop.

    Exposes a BaseServer-like surface (``server_address``, ``serve_forever``,
    ``shutdown``, ``server_close``) so callers/tests don't care it's async.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 upstream_host: str = UPSTREAM_HOST, upstream_port: int = UPSTREAM_PORT):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self._ssl = ssl.create_default_context()
        self._pool: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self._loop = asyncio.new_event_loop()
        # Bind synchronously so server_address (and the chosen port) is known now.
        self._server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, host, port, limit=_READ_LIMIT))
        self.server_address = self._server.sockets[0].getsockname()[:2]

    # -- upstream keep-alive pool (single loop → no lock needed) --------------
    async def _open_upstream(self):
        return await asyncio.open_connection(
            self.upstream_host, self.upstream_port,
            ssl=self._ssl, server_hostname=self.upstream_host)

    async def _acquire_upstream(self):
        """Return (reader, writer, from_pool). A pooled connection may be silently
        dead (network/VPN flap) — the caller retries once on a fresh one."""
        while self._pool:
            reader, writer = self._pool.pop()
            if not writer.is_closing():
                return reader, writer, True
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        reader, writer = await self._open_upstream()
        return reader, writer, False

    def _release_upstream(self, reader, writer) -> None:
        if len(self._pool) < _MAX_POOL and not writer.is_closing():
            self._pool.append((reader, writer))
        else:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _read_head(self, reader):
        try:
            data = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, OSError):
            return None
        text = data[:-4].decode("latin-1")
        start_line = text.split("\r\n", 1)[0]
        return start_line, _header_lines(text)

    async def _read_body_raw(self, reader, hdr: dict) -> Optional[bytes]:
        """Buffer a request body (small: JSON prompt) as raw wire bytes so the
        request can be re-sent if a stale pooled connection fails. Returns None
        if the client disconnects mid-body."""
        te = hdr.get("transfer-encoding", "").lower()
        if "chunked" in te:
            buf = bytearray()
            while True:
                line = await reader.readline()
                if not line:
                    return None
                buf += line
                try:
                    size = int(line.strip().split(b";")[0].decode("latin-1") or "0", 16)
                except ValueError:
                    return None
                if size == 0:
                    while True:
                        t = await reader.readline()
                        buf += t
                        if t in (b"\r\n", b"\n", b""):
                            break
                    return bytes(buf)
                try:
                    buf += await reader.readexactly(size + 2)
                except asyncio.IncompleteReadError:
                    return None
        cl = hdr.get("content-length")
        if cl is not None:
            try:
                n = int(cl)
            except ValueError:
                n = 0
            if n <= 0:
                return b""
            try:
                return await reader.readexactly(n)
            except asyncio.IncompleteReadError:
                return None
        return b""

    def _build_request(self, method, path, req_headers, body: bytes) -> bytes:
        out = [f"{method} {path} HTTP/1.1\r\n".encode("latin-1"),
               f"Host: {self.upstream_host}\r\n".encode("latin-1")]
        for name, value in req_headers:
            low = name.lower()
            if low in _HOP_BY_HOP or low == "host":
                continue
            out.append(f"{name}: {value}\r\n".encode("latin-1"))
        out.append(b"\r\n")
        out.append(body)
        return b"".join(out)

    async def _send_with_retry(self, req_bytes: bytes):
        """Send the buffered request, retrying ONCE on a fresh connection only if
        a *pooled* (reused) connection failed before any response byte — the
        request never reached the server, so a single retry is safe. A fresh
        connection failing means the network is down: give up (never hammer the
        API). Returns (reader, writer, resp_head) or (None, None, None)."""
        try:
            reader, writer, from_pool = await self._acquire_upstream()
        except (ConnectionError, OSError):
            return None, None, None   # cannot open a connection → network down
        try:
            writer.write(req_bytes)
            await writer.drain()
            head = await self._read_head(reader)
            if head is not None:
                return reader, writer, head
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
        if not from_pool:
            return None, None, None   # fresh conn failed → network down, don't retry
        # Stale pooled connection → one retry on a brand-new connection.
        try:
            reader, writer = await self._open_upstream()
            writer.write(req_bytes)
            await writer.drain()
            head = await self._read_head(reader)
            if head is not None:
                return reader, writer, head
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        return None, None, None

    @staticmethod
    async def _send_error(writer, code: int, reason: str, message: str) -> None:
        body = json.dumps({"type": "error",
                           "error": {"type": "api_error", "message": message}}).encode()
        try:
            writer.write(f"HTTP/1.1 {code} {reason}\r\n".encode("latin-1"))
            writer.write(b"content-type: application/json\r\n")
            writer.write(f"content-length: {len(body)}\r\n\r\n".encode("latin-1"))
            writer.write(body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _relay_body(self, reader, writer, hdr: dict, is_request: bool) -> bool:
        """Relay a message body per its framing. Returns True if the body was
        fully framed (connection stays clean/reusable), False on EOF framing."""
        te = hdr.get("transfer-encoding", "").lower()
        if "chunked" in te:
            while True:
                line = await reader.readline()
                if not line:
                    return False
                writer.write(line)
                try:
                    size = int(line.strip().split(b";")[0].decode("latin-1") or "0", 16)
                except ValueError:
                    return False
                if size == 0:
                    while True:  # optional trailers, then the terminating blank line
                        t = await reader.readline()
                        writer.write(t)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    await writer.drain()
                    return True
                chunk = await reader.readexactly(size + 2)  # data + CRLF
                writer.write(chunk)
                await writer.drain()
        cl = hdr.get("content-length")
        if cl is not None:
            try:
                remaining = int(cl)
            except ValueError:
                remaining = 0
            while remaining > 0:
                chunk = await reader.read(min(65536, remaining))
                if not chunk:
                    return False
                writer.write(chunk)
                remaining -= len(chunk)
                await writer.drain()
            return True
        if is_request:
            return True  # no body to relay
        # Response without explicit framing → read to EOF (not reusable).
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        return False

    async def _handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter):
        ureader = uwriter = None
        reuse = False
        try:
            head = await self._read_head(creader)
            if head is None:
                return
            start_line, req_headers = head
            parts = start_line.split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            # Buffer the (small) request body so a stale pooled connection can be
            # retried safely without re-reading from the client.
            body = await self._read_body_raw(creader, _lower(req_headers))
            if body is None:
                return  # client vanished mid-body
            req_bytes = self._build_request(method, path, req_headers, body)

            ureader, uwriter, rhead = await self._send_with_retry(req_bytes)
            if rhead is None:
                # Upstream unreachable (network/VPN down) — hand CC a clean 502
                # instead of a dropped socket, so its own error/retry path runs.
                await self._send_error(cwriter, 502, "Bad Gateway",
                                       "claudex proxy: upstream api.anthropic.com unreachable")
                return

            r_start, r_headers = rhead
            cwriter.write((r_start + "\r\n").encode("latin-1"))
            for name, value in r_headers:
                if name.lower() in _HOP_BY_HOP:
                    continue
                cwriter.write(f"{name}: {value}\r\n".encode("latin-1"))
            cwriter.write(b"\r\n")
            await cwriter.drain()

            rhdr = _lower(r_headers)
            clean = await self._relay_body(ureader, cwriter, rhdr, is_request=False)
            reuse = (clean and "close" not in rhdr.get("connection", "").lower()
                     and r_start.startswith("HTTP/1.1"))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if uwriter is not None:
                if reuse:
                    self._release_upstream(ureader, uwriter)
                else:
                    try:
                        uwriter.close()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                cwriter.close()
            except Exception:  # noqa: BLE001
                pass

    # -- BaseServer-like lifecycle -------------------------------------------
    def serve_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._server.close()
                self._loop.run_until_complete(self._server.wait_closed())
            except Exception:  # noqa: BLE001
                pass
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass

    def server_close(self) -> None:
        try:
            self._server.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if not self._loop.is_running() and not self._loop.is_closed():
                self._loop.close()
        except Exception:  # noqa: BLE001
            pass


def make_server(host: str = "127.0.0.1", port: int = 0,
                upstream_host: str = UPSTREAM_HOST,
                upstream_port: int = UPSTREAM_PORT) -> AsyncProxyServer:
    """Create (but do not run) the reverse proxy. ``port=0`` picks a free port."""
    return AsyncProxyServer(host, port, upstream_host, upstream_port)


def base_url_for(server: AsyncProxyServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


def serve_forever(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Blocking: run the proxy until interrupted (used by ``claudex proxy``)."""
    server = make_server(host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def start_background(host: str = "127.0.0.1", port: int = 0
                     ) -> tuple[AsyncProxyServer, threading.Thread, str]:
    """Start the proxy on a daemon thread; return (server, thread, base_url)."""
    server = make_server(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, base_url_for(server)


# ---------------------------------------------------------------------------
# Shared proxy daemon — one long-lived proxy reused by every proxy-mode launch
# (use / run / fleet), so 100 concurrent `claude -p` share a single forwarder.
# ---------------------------------------------------------------------------

def _read_liveness() -> Optional[dict]:
    try:
        d = json.loads(PROXY_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("url") and d.get("pid"):
            return d
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def status() -> dict:
    """Return {running, url, pid, host, port} for the shared proxy daemon."""
    info = _read_liveness()
    if info and _pid_alive(int(info["pid"])) and _port_open(
            info.get("host", "127.0.0.1"), int(info["port"])):
        return {"running": True, **info}
    return {"running": False}


def run_daemon(host: str = "127.0.0.1", port: int = 0) -> None:
    """Bind, publish liveness, and serve forever (the detached daemon body)."""
    server = make_server(host, port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}"
    PROXY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROXY_FILE.write_text(json.dumps(
        {"pid": os.getpid(), "host": bound_host, "port": bound_port, "url": url}),
        encoding="utf-8")
    try:
        PROXY_FILE.chmod(0o600)
    except OSError:
        pass

    # Signal handler just stops the loop; serve_forever() then returns and the
    # daemon exits naturally (no sys.exit from inside the handler, which doesn't
    # reliably terminate a process blocked in the event loop → orphans).
    def _stop(*_a):
        server.shutdown()

    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None), signal.SIGINT):
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    try:
        server.serve_forever()
    finally:
        try:
            cur = _read_liveness()
            if cur and int(cur.get("pid", -1)) == os.getpid():
                PROXY_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _claudex_command() -> list[str]:
    # Prefer the current interpreter/module so the daemon is always the same
    # version as the caller (a PATH `claudex` can resolve to a different install
    # that doesn't understand `--daemon`). Fall back to the console script.
    try:
        import claudex  # noqa: F401
        return [sys.executable, "-m", "claudex.cli"]
    except Exception:  # noqa: BLE001
        exe = shutil.which("claudex")
        return [exe] if exe else [sys.executable, "-m", "claudex.cli"]


LOCK_FILE = CLAUDEX_HOME / ".proxy.lock"


def _spawn_daemon(host: str) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(_claudex_command() + ["proxy", "--daemon", "--host", host], **kwargs)


def ensure_shared(host: str = "127.0.0.1", timeout: float = 8.0) -> Optional[str]:
    """Return the shared daemon's base URL, auto-starting a detached one if needed.

    Cold start is serialized with a file lock so that many simultaneous launches
    (e.g. 100 `claude -p` at once with no daemon running) start exactly ONE
    daemon and all reuse it — never a herd of orphaned proxies.
    """
    st = status()
    if st["running"]:
        return st["url"]

    lock_fd = None
    try:
        CLAUDEX_HOME.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
        if not IS_WINDOWS:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (OSError, ImportError):
                pass  # best-effort: proceed without the lock
        # Re-check inside the lock: another launch may have just started it.
        st = status()
        if st["running"]:
            return st["url"]
        try:
            _spawn_daemon(host)
        except OSError:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = status()
            if st["running"]:
                return st["url"]
            time.sleep(0.15)
        return None
    finally:
        if lock_fd is not None:
            try:
                if not IS_WINDOWS:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def stop_shared() -> bool:
    """Stop the shared proxy daemon if running. Returns True if one was stopped."""
    info = _read_liveness()
    if not info:
        return False
    pid = int(info.get("pid", -1))
    stopped = False
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
    try:
        PROXY_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return stopped
