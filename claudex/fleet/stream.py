"""Live streaming sessions (mode B — mailbox).

A StreamSession keeps a single `claude` process alive and feeds it prompts over
time via Claude Code's realtime streaming protocol:

    claude -p --input-format stream-json --output-format stream-json

Each prompt is written to the process's stdin as one NDJSON user message; the
process keeps the conversation in memory (no transcript reload, MCP stays warm)
and emits NDJSON events on stdout. A background reader thread decodes those
events and hands each finished turn's `result` event back to the caller.

On top of that primitive, a *mailbox* lets other processes inject prompts into a
running session: the host (`claudex fleet live`) reads prompt lines from a FIFO
and `claudex fleet tell <id>` appends to it. The FIFO is POSIX-only; on Windows
use mode C's socket transport instead.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from claudex.constants import CLAUDE_BIN, FLEET_DIR, IS_WINDOWS


# ── stream event parsing ──────────────────────────────────────────────────────
def parse_stream_line(line: str) -> Optional[dict]:
    """Parse one NDJSON line from the stream-json output. None for blank/garbage."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def encode_user_message(prompt: str) -> str:
    """One NDJSON user message for stream-json input (newline-terminated)."""
    msg = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }
    return json.dumps(msg) + "\n"


@dataclass
class TurnResult:
    """A finished turn, distilled from the stream's `result` event."""

    text: str = ""
    is_error: bool = False
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    duration_ms: Optional[int] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_event(cls, ev: dict) -> "TurnResult":
        return cls(
            text=str(ev.get("result", "") or ""),
            is_error=bool(ev.get("is_error")),
            session_id=ev.get("session_id"),
            cost_usd=ev.get("total_cost_usd"),
            num_turns=ev.get("num_turns"),
            duration_ms=ev.get("duration_ms"),
            raw=ev,
        )


# ── the live session ──────────────────────────────────────────────────────────
def build_stream_argv(model: Optional[str] = None,
                      extra_args: Optional[list[str]] = None) -> list[str]:
    argv = [
        CLAUDE_BIN, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",  # stream-json output requires verbose to emit per-event lines
    ]
    if model:
        argv += ["--model", model]
    argv += list(extra_args or [])
    return argv


class StreamSession:
    """A persistent `claude` streaming process you can feed prompts over time.

    Construct via `StreamSession.spawn(...)` for the real process, or directly with
    a process-like object (duck-typed: `.stdin.write/.flush`, `.stdout` iterable of
    lines, `.poll()`, `.wait()`, `.terminate()`) for tests.
    """

    def __init__(self, proc, *, log_file: Optional[Path] = None,
                 on_event: Optional[Callable[[dict], None]] = None) -> None:
        self._proc = proc
        self._log = open(log_file, "a", encoding="utf-8") if log_file else None
        self._on_event = on_event
        self._results: "queue.Queue[TurnResult]" = queue.Queue()
        self.session_id: Optional[str] = None
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @classmethod
    def spawn(cls, env: Optional[dict] = None, cwd: Optional[str] = None,
              model: Optional[str] = None, extra_args: Optional[list[str]] = None,
              log_file: Optional[Path] = None,
              on_event: Optional[Callable[[dict], None]] = None) -> "StreamSession":
        argv = build_stream_argv(model=model, extra_args=extra_args)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            text=True,
            bufsize=1,  # line-buffered
        )
        return cls(proc, log_file=log_file, on_event=on_event)

    # ── reader ────────────────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        stdout: Iterator[str] = self._proc.stdout  # type: ignore[assignment]
        if stdout is None:
            return
        for line in stdout:
            if self._log:
                try:
                    self._log.write(line if line.endswith("\n") else line + "\n")
                    self._log.flush()
                except Exception:
                    pass
            ev = parse_stream_line(line)
            if ev is None:
                continue
            # Capture the session id from init (and keep it fresh from results).
            sid = ev.get("session_id")
            if sid:
                self.session_id = sid
            if self._on_event:
                try:
                    self._on_event(ev)
                except Exception:
                    pass
            if ev.get("type") == "result":
                self._results.put(TurnResult.from_event(ev))

    # ── interaction ─────────────────────────────────────────────────────────--
    def send(self, prompt: str) -> None:
        """Queue a prompt into the live session (does not wait for the reply)."""
        if self._closed:
            raise RuntimeError("session is closed")
        stdin = self._proc.stdin
        if stdin is None:
            raise RuntimeError("session has no stdin")
        stdin.write(encode_user_message(prompt))
        stdin.flush()

    def wait_turn(self, timeout: Optional[float] = None) -> TurnResult:
        """Block until the next turn's result arrives."""
        return self._results.get(timeout=timeout)

    def ask(self, prompt: str, timeout: Optional[float] = None) -> TurnResult:
        """Send a prompt and wait for that turn's result."""
        self.send(prompt)
        return self.wait_turn(timeout=timeout)

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def close(self, grace: float = 5.0) -> int:
        """Close stdin so the process exits; return its exit code."""
        if self._closed:
            return self._proc.poll() or 0
        self._closed = True
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            code = self._proc.wait(timeout=grace)
        except Exception:
            try:
                self._proc.terminate()
                code = self._proc.wait(timeout=grace)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                code = self._proc.poll() or -9
        self._reader.join(timeout=grace)
        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
        return code or 0

    def __enter__(self) -> "StreamSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── mailbox registry (FIFO-based) ─────────────────────────────────────────────
LIVE_DIR = FLEET_DIR / "live"


def _ensure_live_dir() -> Path:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return LIVE_DIR


def fifo_path(session_id: str) -> Path:
    return LIVE_DIR / f"{session_id}.in"


def registry_path(session_id: str) -> Path:
    return LIVE_DIR / f"{session_id}.json"


def live_log_path(session_id: str) -> Path:
    return LIVE_DIR / f"{session_id}.log"


def register_live(session_id: str, meta: dict) -> None:
    _ensure_live_dir()
    registry_path(session_id).write_text(json.dumps(meta), encoding="utf-8")


def unregister_live(session_id: str) -> None:
    for p in (registry_path(session_id), fifo_path(session_id)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def list_live() -> list[dict]:
    if not LIVE_DIR.exists():
        return []
    out = []
    for p in sorted(LIVE_DIR.glob("*.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = meta.get("pid")
        meta["alive"] = _pid_alive(pid)
        out.append(meta)
    return out


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours — still alive
    except Exception:
        return False
    return True


def make_fifo(session_id: str) -> Path:
    """Create the session's mailbox FIFO (POSIX only). Returns its path."""
    if IS_WINDOWS:
        raise RuntimeError("live mailbox (FIFO) is POSIX-only; use mode C on Windows")
    _ensure_live_dir()
    path = fifo_path(session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    os.mkfifo(path, 0o600)
    return path


def tell(session_id: str, prompt: str, timeout: float = 5.0) -> None:
    """Append a prompt to a live session's mailbox FIFO.

    Opening a FIFO for writing blocks until a reader is attached, so this is
    bounded by *timeout* to fail fast when the session host is gone.
    """
    path = fifo_path(session_id)
    if not path.exists():
        raise FileNotFoundError(f"no live session {session_id} (mailbox {path} missing)")
    line = prompt.replace("\n", " ").rstrip() + "\n"
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"live session {session_id} has no reader attached "
                    f"(is `claudex fleet live` still running?)"
                )
            time.sleep(0.1)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
