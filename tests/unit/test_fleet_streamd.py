"""Unit tests for the resident stream daemon (mode C)."""

import asyncio

import pytest

from claudex.fleet import streamd
from claudex.fleet.stream import TurnResult


# ── liveness / client guards ────────────────────────────────────────────────────
def test_status_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(streamd, "STREAMD_FILE", tmp_path / ".streamd")
    assert streamd.status() == {"running": False}


def test_send_command_no_daemon_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(streamd, "STREAMD_FILE", tmp_path / ".streamd")
    resp = streamd.send_command({"cmd": "ping"}, auto_start=False)
    assert resp["ok"] is False
    assert "not running" in resp["error"]


# ── manager with fake sessions ──────────────────────────────────────────────────
class _FakeSession:
    def __init__(self):
        self.session_id = "sess-fake"
        self.sent = []
        self.interrupted = False
        self.closed = False
        self._mem = None

    def ask(self, prompt, timeout=None):
        # Emulate memory: remember a number, recall it.
        if "remember" in prompt.lower():
            self._mem = "".join(c for c in prompt if c.isdigit())
            return TurnResult(text="noted", session_id=self.session_id)
        return TurnResult(text=self._mem or "idk", session_id=self.session_id)

    def send(self, prompt):
        self.sent.append(prompt)

    def interrupt(self):
        self.interrupted = True

    def is_alive(self):
        return not self.closed

    def close(self, grace=5.0):
        self.closed = True
        return 0


class _FakeSpawner:
    last = None

    @staticmethod
    def spawn(*a, **k):
        s = _FakeSession()
        _FakeSpawner.last = s
        return s


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setattr(streamd, "StreamSession", _FakeSpawner)
    m = streamd._SessionManager()
    monkeypatch.setattr(m, "_build_env", lambda profile: {})
    return m


async def test_open_ask_list_close(mgr):
    opened = await mgr.open("work", "haiku", "/tmp")
    assert opened["ok"] and opened["id"]
    sid = opened["id"]

    r1 = await mgr.ask(sid, "Remember 4729.", 10)
    assert r1["ok"] and r1["text"] == "noted"
    r2 = await mgr.ask(sid, "What number?", 10)
    assert r2["text"] == "4729"  # context persisted in the same session

    listing = mgr.list()
    assert listing["sessions"][0]["id"] == sid
    assert listing["sessions"][0]["turns"] == 2
    assert listing["sessions"][0]["alive"] is True

    closed = await mgr.close(sid)
    assert closed["ok"] and closed["turns"] == 2
    assert mgr.list()["sessions"] == []


async def test_tell_and_interrupt(mgr):
    sid = (await mgr.open("work", None, None))["id"]
    assert (await mgr.tell(sid, "hi"))["ok"]
    assert _FakeSpawner.last.sent == ["hi"]
    assert (await mgr.interrupt(sid))["ok"]
    assert _FakeSpawner.last.interrupted is True


async def test_ask_unknown_session(mgr):
    resp = await mgr.ask("nope", "x", 5)
    assert resp["ok"] is False and "no session" in resp["error"]


# ── dispatch routing ────────────────────────────────────────────────────────────
async def test_dispatch_routes_commands(mgr):
    stop = asyncio.Event()
    assert (await streamd._dispatch(mgr, {"cmd": "ping"}, stop))["pid"]
    opened = await streamd._dispatch(mgr, {"cmd": "open", "profile": "work"}, stop)
    sid = opened["id"]
    asked = await streamd._dispatch(
        mgr, {"cmd": "ask", "id": sid, "prompt": "Remember 11.", "timeout": 5}, stop)
    assert asked["text"] == "noted"
    assert (await streamd._dispatch(mgr, {"cmd": "list"}, stop))["sessions"]
    assert (await streamd._dispatch(mgr, {"cmd": "shutdown"}, stop))["shutdown"]
    assert stop.is_set()


async def test_dispatch_unknown_cmd(mgr):
    resp = await streamd._dispatch(mgr, {"cmd": "frobnicate"}, asyncio.Event())
    assert resp["ok"] is False and "unknown" in resp["error"]
