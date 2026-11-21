"""Resident stream daemon (mode C).

One long-lived process hosts MANY live `claude` streaming sessions (see
claudex.fleet.stream.StreamSession). Unlike mode B — where the session dies when
the hosting `fleet live` command exits — these sessions outlive the CLI
invocation that created them: you `fleet open` a session, then `fleet ask`,
`fleet interrupt`, `fleet close` it from separate commands, each a short client
that talks to the daemon over a local Unix socket.

Transport: newline-delimited JSON over a Unix domain socket (POSIX-only, local).
Lifecycle mirrors the shared proxy daemon: a pidfile, flock-serialized cold start,
clean signal shutdown that closes every session.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME, FLEET_DIR, IS_WINDOWS
from claudex.fleet.models import new_job_id
from claudex.fleet.stream import StreamSession

STREAMD_FILE = FLEET_DIR / ".streamd"
SOCK_PATH = FLEET_DIR / "streamd.sock"
LOCK_FILE = FLEET_DIR / ".streamd.lock"


# ── liveness ───────────────────────────────────────────────────────────────────
def _read_liveness() -> Optional[dict]:
    try:
        d = json.loads(STREAMD_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("pid") and d.get("sock"):
            return d
    except (OSError, ValueError):
        return None
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


def _sock_live(path: str) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


def status() -> dict:
    """Return {running, pid, sock} for the resident stream daemon."""
    info = _read_liveness()
    if info and _pid_alive(int(info["pid"])) and _sock_live(info["sock"]):
        return {"running": True, **info}
    return {"running": False}


# ── the daemon ─────────────────────────────────────────────────────────────────
class _SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, StreamSession] = {}
        self.meta: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def _build_env(self, profile: str):
        from claudex.core.auth import AuthManager
        from claudex.core.profile import ProfileManager
        from claudex.fleet.runner import build_claude_env
        prof = ProfileManager().get(profile)
        return build_claude_env(AuthManager(), profile, prof.config_dir)

    async def open(self, profile: str, model: Optional[str], cwd: Optional[str]) -> dict:
        from claudex.fleet import stream as _stream
        sid = new_job_id()
        env = await asyncio.to_thread(self._build_env, profile)
        log_file = _stream.live_log_path(sid)
        sess = await asyncio.to_thread(
            StreamSession.spawn, env, cwd, model, None, log_file, None,
        )
        self.sessions[sid] = sess
        self.locks[sid] = asyncio.Lock()
        self.meta[sid] = {
            "id": sid, "profile": profile, "model": model,
            "cwd": cwd or os.getcwd(), "created_at": time.time(), "turns": 0,
        }
        return {"ok": True, "id": sid}

    async def ask(self, sid: str, prompt: str, timeout: float) -> dict:
        sess = self.sessions.get(sid)
        if sess is None:
            return {"ok": False, "error": f"no session {sid}"}
        lock = self.locks[sid]
        async with lock:  # one turn at a time per session
            try:
                res = await asyncio.to_thread(sess.ask, prompt, timeout)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"turn failed: {e}"}
        self.meta[sid]["turns"] += 1
        self.meta[sid]["session_id"] = sess.session_id
        return {"ok": True, "text": res.text, "is_error": res.is_error,
                "session_id": res.session_id, "cost_usd": res.cost_usd,
                "num_turns": res.num_turns}

    async def tell(self, sid: str, prompt: str) -> dict:
        """Fire-and-forget send (does not wait for the reply)."""
        sess = self.sessions.get(sid)
        if sess is None:
            return {"ok": False, "error": f"no session {sid}"}
        await asyncio.to_thread(sess.send, prompt)
        return {"ok": True}

    async def interrupt(self, sid: str) -> dict:
        sess = self.sessions.get(sid)
        if sess is None:
            return {"ok": False, "error": f"no session {sid}"}
        await asyncio.to_thread(sess.interrupt)
        return {"ok": True}

    async def close(self, sid: str) -> dict:
        sess = self.sessions.pop(sid, None)
        self.locks.pop(sid, None)
        meta = self.meta.pop(sid, None)
        if sess is None:
            return {"ok": False, "error": f"no session {sid}"}
        code = await asyncio.to_thread(sess.close)
        from claudex.fleet import stream as _stream
        _stream.unregister_live(sid)
        return {"ok": True, "exit_code": code, "turns": (meta or {}).get("turns", 0)}

    def list(self) -> dict:
        rows = []
        for sid, m in self.meta.items():
            sess = self.sessions.get(sid)
            rows.append({**m, "alive": bool(sess and sess.is_alive()),
                         "session_id": (sess.session_id if sess else None)})
        return {"ok": True, "sessions": rows}

    async def close_all(self) -> None:
        for sid in list(self.sessions.keys()):
            try:
                await self.close(sid)
            except Exception:  # noqa: BLE001
                pass


async def _serve(mgr: _SessionManager, stop: asyncio.Event) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:  # noqa: BLE001
                resp = {"ok": False, "error": "bad json"}
            else:
                resp = await _dispatch(mgr, req, stop)
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # Fresh socket path.
    try:
        SOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(handle, path=str(SOCK_PATH))
    try:
        os.chmod(SOCK_PATH, 0o600)
    except OSError:
        pass
    async with server:
        await stop.wait()
    await mgr.close_all()


async def _dispatch(mgr: _SessionManager, req: dict, stop: asyncio.Event) -> dict:
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True, "pid": os.getpid()}
    if cmd == "open":
        return await mgr.open(req.get("profile"), req.get("model"), req.get("cwd"))
    if cmd == "ask":
        return await mgr.ask(req.get("id"), req.get("prompt", ""),
                             float(req.get("timeout", 600)))
    if cmd == "tell":
        return await mgr.tell(req.get("id"), req.get("prompt", ""))
    if cmd == "interrupt":
        return await mgr.interrupt(req.get("id"))
    if cmd == "close":
        return await mgr.close(req.get("id"))
    if cmd == "list":
        return mgr.list()
    if cmd == "shutdown":
        stop.set()
        return {"ok": True, "shutdown": True}
    return {"ok": False, "error": f"unknown cmd {cmd!r}"}


def run_daemon() -> None:
    """The detached daemon body: serve the control socket until signalled."""
    if IS_WINDOWS:
        raise RuntimeError("stream daemon is POSIX-only (Unix socket)")
    FLEET_DIR.mkdir(parents=True, exist_ok=True)
    STREAMD_FILE.write_text(json.dumps(
        {"pid": os.getpid(), "sock": str(SOCK_PATH)}), encoding="utf-8")
    try:
        STREAMD_FILE.chmod(0o600)
    except OSError:
        pass

    async def main() -> None:
        mgr = _SessionManager()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, stop.set)
                except (NotImplementedError, ValueError, OSError):
                    pass
        await _serve(mgr, stop)

    try:
        asyncio.run(main())
    finally:
        cur = _read_liveness()
        if cur and int(cur.get("pid", -1)) == os.getpid():
            STREAMD_FILE.unlink(missing_ok=True)
        try:
            SOCK_PATH.unlink()
        except FileNotFoundError:
            pass


# ── client ─────────────────────────────────────────────────────────────────────
def _claudex_command() -> list[str]:
    try:
        import claudex  # noqa: F401
        return [sys.executable, "-m", "claudex.cli"]
    except Exception:  # noqa: BLE001
        import shutil
        exe = shutil.which("claudex")
        return [exe] if exe else [sys.executable, "-m", "claudex.cli"]


def _spawn_daemon() -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "start_new_session": True}
    subprocess.Popen(_claudex_command() + ["fleet", "_streamd-run"], **kwargs)


def ensure_daemon(timeout: float = 8.0) -> Optional[str]:
    """Return the daemon's socket path, auto-starting a detached one if needed."""
    if IS_WINDOWS:
        return None
    st = status()
    if st["running"]:
        return st["sock"]
    lock_fd = None
    try:
        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except (OSError, ImportError):
            pass
        st = status()  # re-check inside the lock
        if st["running"]:
            return st["sock"]
        try:
            _spawn_daemon()
        except OSError:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = status()
            if st["running"]:
                return st["sock"]
            time.sleep(0.15)
        return None
    finally:
        if lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def send_command(obj: dict, timeout: float = 620.0,
                 auto_start: bool = True) -> dict:
    """Send one command to the daemon and return its JSON response."""
    if IS_WINDOWS:
        return {"ok": False, "error": "stream daemon is POSIX-only"}
    sock = ensure_daemon() if auto_start else (status().get("sock"))
    if not sock:
        return {"ok": False, "error": "stream daemon not running"}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock)
    except OSError as e:
        return {"ok": False, "error": f"cannot reach daemon: {e}"}
    try:
        s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        return {"ok": False, "error": f"daemon io error: {e}"}
    finally:
        s.close()
    if not buf:
        return {"ok": False, "error": "empty response from daemon"}
    try:
        return json.loads(buf.decode("utf-8").split("\n", 1)[0])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"bad response: {e}"}


def stop_daemon() -> bool:
    """Stop the resident daemon if running. Returns True if one was stopped."""
    info = _read_liveness()
    if not info:
        return False
    # Ask it to close sessions cleanly, then ensure it's gone.
    if status()["running"]:
        send_command({"cmd": "shutdown"}, timeout=10, auto_start=False)
    pid = int(info.get("pid", -1))
    stopped = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            stopped = True
            break
        time.sleep(0.1)
    if not stopped and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
    STREAMD_FILE.unlink(missing_ok=True)
    try:
        SOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    return stopped
