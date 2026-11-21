"""Unit tests for live streaming sessions (mode B)."""

import json
import os
import threading

import pytest

from claudex.fleet import stream


# ── pure helpers ───────────────────────────────────────────────────────────────
def test_build_stream_argv_has_streaming_flags():
    argv = stream.build_stream_argv(model="haiku")
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv  # required for per-event output
    assert argv[argv.index("--model") + 1] == "haiku"


def test_encode_user_message_is_ndjson():
    line = stream.encode_user_message("hello")
    assert line.endswith("\n")
    obj = json.loads(line)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"][0]["text"] == "hello"


def test_parse_stream_line_ignores_garbage():
    assert stream.parse_stream_line("") is None
    assert stream.parse_stream_line("not json") is None
    assert stream.parse_stream_line("[1,2,3]") is None  # not a dict
    assert stream.parse_stream_line('{"type":"result"}') == {"type": "result"}


def test_turn_result_from_event():
    ev = {"type": "result", "result": "42", "is_error": False,
          "session_id": "s1", "total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 123}
    r = stream.TurnResult.from_event(ev)
    assert r.text == "42" and not r.is_error
    assert r.session_id == "s1" and r.cost_usd == 0.01
    assert r.num_turns == 2 and r.duration_ms == 123


# ── StreamSession against a fake process ───────────────────────────────────────
class _FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, s):
        self.writes.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = iter(lines)
        self._code = None

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        self._code = 0
        return 0

    def terminate(self):
        self._code = -15

    def kill(self):
        self._code = -9


def test_session_reads_result_and_session_id():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}) + "\n",
        json.dumps({"type": "result", "result": "noted", "session_id": "sess-1"}) + "\n",
    ]
    sess = stream.StreamSession(_FakeProc(lines))
    res = sess.wait_turn(timeout=2)
    assert res.text == "noted"
    assert sess.session_id == "sess-1"
    sess.close()


def test_session_send_writes_ndjson_to_stdin():
    proc = _FakeProc([json.dumps({"type": "result", "result": "ok"}) + "\n"])
    sess = stream.StreamSession(proc)
    res = sess.ask("do a thing", timeout=2)
    assert res.text == "ok"
    assert len(proc.stdin.writes) == 1
    sent = json.loads(proc.stdin.writes[0])
    assert sent["message"]["content"][0]["text"] == "do a thing"
    sess.close()


def test_session_close_closes_stdin():
    proc = _FakeProc([])
    sess = stream.StreamSession(proc)
    sess.close()
    assert proc.stdin.closed


# ── mailbox registry ───────────────────────────────────────────────────────────
@pytest.fixture
def live_dir(tmp_path, monkeypatch):
    d = tmp_path / "live"
    monkeypatch.setattr(stream, "LIVE_DIR", d)
    return d


def test_register_list_unregister(live_dir):
    stream.register_live("abc", {"id": "abc", "profile": "work", "pid": os.getpid()})
    rows = stream.list_live()
    assert len(rows) == 1 and rows[0]["id"] == "abc"
    assert rows[0]["alive"] is True  # our own pid
    stream.unregister_live("abc")
    assert stream.list_live() == []


def test_list_live_marks_dead_pid(live_dir):
    stream.register_live("x", {"id": "x", "profile": "p", "pid": 999999})
    rows = stream.list_live()
    assert rows[0]["alive"] is False


def test_tell_missing_session_raises(live_dir):
    with pytest.raises(FileNotFoundError):
        stream.tell("nope", "hi")


@pytest.mark.skipif(os.name == "nt", reason="FIFO is POSIX-only")
def test_tell_writes_to_fifo(live_dir):
    fifo = stream.make_fifo("sess")
    got = []
    ready = threading.Event()

    def reader():
        fd = os.open(str(fifo), os.O_RDWR)
        ready.set()
        with os.fdopen(fd, "r") as f:
            got.append(f.readline().strip())

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    ready.wait(timeout=2)
    stream.tell("sess", "hello mailbox")
    t.join(timeout=2)
    assert got == ["hello mailbox"]
