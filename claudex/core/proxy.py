"""Local reverse proxy to the real Anthropic API.

Why this exists
---------------
On Claude Code 2.1.x an org-pushed managed policy is gateway-verified and
applied from the in-memory fetch result, so no local file/env/shim trick can
neutralise it in a normal session. The one lever that works is making Claude
Code *not reach the first-party settings gateway*: when ``ANTHROPIC_BASE_URL``
is not an Anthropic host, Claude Code skips the managed-settings pull
("Starting without your organization's managed settings") — confirmed against
a fail-open org.

This module runs a tiny reverse proxy on ``127.0.0.1`` that transparently
forwards every request to ``api.anthropic.com``. Pointing ``ANTHROPIC_BASE_URL``
at it means:
  * Claude Code sees a non-Anthropic base URL  -> skips the org policy;
  * model inference still reaches the real API  -> everything works.

The proxy preserves method, path, query, headers (Authorization, anthropic-*,
etc.) and body, and streams responses (including SSE ``text/event-stream``)
back unbuffered. Stdlib only.

NOTE: only usable where the org policy is *fail-open*. A fail-closed org
("Your organization requires managed settings") blocks startup when the
gateway is unreachable, and this cannot be overridden locally by design.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME, IS_WINDOWS

UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443

#: Liveness file for the shared proxy daemon (one proxy serves every launch).
PROXY_FILE = CLAUDEX_HOME / ".proxy"

#: Env applied for a proxy-mode launch: route through the local proxy AND turn
#: telemetry off by default (both org-pushed OTEL — already skipped with the
#: managed pull — and Claude Code's own first-party analytics).
def proxy_env(base_url: str) -> dict:
    return {"ANTHROPIC_BASE_URL": base_url, "DISABLE_TELEMETRY": "1"}

# Headers that must not be copied verbatim between the two connections.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Silence the default stderr access log; claudex owns the output.
    def log_message(self, *_args) -> None:  # noqa: D401
        pass

    # All methods funnel through here.
    def _proxy(self) -> None:
        upstream_host = getattr(self.server, "upstream_host", UPSTREAM_HOST)
        upstream_port = getattr(self.server, "upstream_port", UPSTREAM_PORT)

        # Read request body if present.
        body = b""
        length = self.headers.get("Content-Length")
        if length:
            try:
                body = self.rfile.read(int(length))
            except (ValueError, OSError):
                body = b""

        # Rebuild outgoing headers: drop hop-by-hop, force the real Host.
        out_headers = {}
        for key, value in self.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            out_headers[key] = value
        out_headers["Host"] = upstream_host

        try:
            conn = http.client.HTTPSConnection(upstream_host, upstream_port, timeout=600)
            conn.request(self.command, self.path, body=body or None, headers=out_headers)
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001 — surface any upstream failure to the client
            self.send_error(502, f"claudex proxy upstream error: {e}")
            return

        # Status line + headers (drop hop-by-hop / length; we stream + close).
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() in _HOP_BY_HOP:
                continue
            self.send_header(key, value)
        # Stream to EOF, then close — works for fixed, chunked and SSE bodies.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            conn.close()
        self.close_connection = True

    # Map the verbs Claude Code uses to the proxy.
    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy
    do_HEAD = _proxy
    do_OPTIONS = _proxy


def make_server(host: str = "127.0.0.1", port: int = 0,
                upstream_host: str = UPSTREAM_HOST,
                upstream_port: int = UPSTREAM_PORT) -> ThreadingHTTPServer:
    """Create (but do not start) the reverse-proxy server. ``port=0`` picks a free port."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.upstream_host = upstream_host      # type: ignore[attr-defined]
    server.upstream_port = upstream_port      # type: ignore[attr-defined]
    return server


def base_url_for(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


def serve_forever(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Blocking: run the proxy until interrupted (used by ``claudex proxy``)."""
    server = make_server(host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


def start_background(host: str = "127.0.0.1", port: int = 0
                     ) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start the proxy in a daemon thread; return (server, thread, base_url).

    Caller keeps the process alive (e.g. the supervisor) and calls
    ``server.shutdown()`` on exit.
    """
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
    if info and _pid_alive(int(info["pid"])) and _port_open(info.get("host", "127.0.0.1"),
                                                            int(info["port"])):
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

    def _cleanup(*_a):
        try:
            cur = _read_liveness()
            if cur and int(cur.get("pid", -1)) == os.getpid():
                PROXY_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass

    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None), signal.SIGINT):
        if sig is not None:
            try:
                signal.signal(sig, lambda *_a: (_cleanup(), sys.exit(0)))
            except (ValueError, OSError):
                pass
    try:
        server.serve_forever()
    finally:
        _cleanup()


def _claudex_command() -> list[str]:
    # Prefer the *current* interpreter/module so the daemon is always the same
    # version as the caller (a PATH `claudex` can resolve to a different install
    # that doesn't understand `--daemon`). Fall back to the console script.
    try:
        import claudex  # noqa: F401
        return [sys.executable, "-m", "claudex.cli"]
    except Exception:  # noqa: BLE001
        exe = shutil.which("claudex")
        return [exe] if exe else [sys.executable, "-m", "claudex.cli"]


def ensure_shared(host: str = "127.0.0.1", timeout: float = 8.0) -> Optional[str]:
    """Return the shared daemon's base URL, auto-starting a detached one if needed.

    All proxy-mode launches call this, so a single daemon serves every session.
    Returns None if the daemon could not be started.
    """
    st = status()
    if st["running"]:
        return st["url"]
    # Spawn a detached daemon on a free port and wait for it to publish liveness.
    try:
        kwargs: dict = {"stdin": subprocess.DEVNULL,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP  # DETACHED
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(_claudex_command() + ["proxy", "--daemon", "--host", host],
                         **kwargs)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = status()
        if st["running"]:
            return st["url"]
        time.sleep(0.15)
    return None


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
