"""Session migrate carries a transcript the way a switch does."""

import json

import pytest

from claudex.history.browser import HistoryBrowser
from claudex.history.parser import parse_session_file

SID = "11111111-2222-3333-4444-555555555555"
PROJECT = "-tmp-proj"


def _assistant(content):
    return {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": SID,
            "message": {"role": "assistant", "content": content}}


def _write_session(config_dir):
    project = config_dir / "projects" / PROJECT
    project.mkdir(parents=True)
    lines = [
        {"type": "user", "uuid": "u1", "sessionId": SID, "cwd": "/tmp/proj",
         "timestamp": "2026-10-16T10:00:00Z",
         "message": {"role": "user", "content": "question"}},
        _assistant([{"type": "thinking", "thinking": "t", "signature": "s"},
                    {"type": "text", "text": "answer"}]),
    ]
    transcript = project / f"{SID}.jsonl"
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    subagents = project / SID / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-x.jsonl").write_text(
        json.dumps(_assistant([{"type": "redacted_thinking", "data": "d"},
                               {"type": "text", "text": "sub"}])) + "\n", encoding="utf-8")
    (project / SID / "tool-results").mkdir()
    (project / SID / "tool-results" / "r.txt").write_text("output", encoding="utf-8")
    return parse_session_file(transcript, "src")


def _block_types(path):
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    return [[b["type"] for b in e["message"]["content"]]
            for e in entries if e["type"] == "assistant"]


def test_migrate_strips_signature_blocks_and_carries_session_dir(tmp_path):
    session = _write_session(tmp_path / "src")
    target = tmp_path / "dst"

    HistoryBrowser([]).migrate_session(session, "dst", target)

    project = target / "projects" / PROJECT
    assert _block_types(project / f"{SID}.jsonl") == [["text"]]
    assert _block_types(project / SID / "subagents" / "agent-x.jsonl") == [["text"]]
    assert (project / SID / "tool-results" / "r.txt").read_text() == "output"
    assert _block_types(session.file_path) == [["thinking", "text"]]  # source untouched


def test_migrate_keep_thinking_copies_verbatim(tmp_path):
    session = _write_session(tmp_path / "src")
    target = tmp_path / "dst"

    HistoryBrowser([]).migrate_session(session, "dst", target, strip=False)

    project = target / "projects" / PROJECT
    assert _block_types(project / f"{SID}.jsonl") == [["thinking", "text"]]
    assert _block_types(project / SID / "subagents" / "agent-x.jsonl") == [["redacted_thinking", "text"]]


def test_migrate_refuses_to_overwrite(tmp_path):
    session = _write_session(tmp_path / "src")
    target = tmp_path / "dst"
    HistoryBrowser([]).migrate_session(session, "dst", target)
    with pytest.raises(FileExistsError):
        HistoryBrowser([]).migrate_session(session, "dst", target)
