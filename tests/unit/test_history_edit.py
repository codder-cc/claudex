"""Tests for trimming conversational rounds off a transcript (history/edit)."""

import json
from pathlib import Path

import pytest

from claudex.history import edit


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _user(uuid, parent, text, **extra):
    o = {"type": "user", "uuid": uuid, "parentUuid": parent,
         "message": {"role": "user", "content": text}}
    o.update(extra)
    return o


def _assistant_text(uuid, parent, text):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _assistant_tool(uuid, parent, tool_id):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}]}}


def _tool_result(uuid, parent, tool_id):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "toolUseResult": {"ok": True},
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "done"}]}}


@pytest.fixture
def transcript(tmp_path) -> Path:
    """A 3-round transcript: each round has a tool call + result, plus trailing
    slash-command plumbing that must NOT count as its own round."""
    f = tmp_path / "sess.jsonl"
    lines = [
        {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}},
        _user("u1", None, "first question"),
        _assistant_tool("a1", "u1", "t1"),
        _tool_result("r1", "a1", "t1"),
        _assistant_text("a1b", "r1", "first answer"),
        _user("u2", "a1b", "second question"),
        _assistant_tool("a2", "u2", "t2"),
        _tool_result("r2", "a2", "t2"),
        _assistant_text("a2b", "r2", "second answer"),
        _user("u3", "a2b", "third question"),
        _assistant_text("a3", "u3", "third answer"),
        # slash-command plumbing hanging off the last round — folds into round 3
        _user("c1", "a3", "<command-name>/exit</command-name>"),
        _user("c2", "c1", "<local-command-stdout>bye</local-command-stdout>"),
    ]
    f.write_text("".join(_line(o) for o in lines))
    return f


def test_list_rounds_counts_real_turns_only(transcript):
    rounds = edit.list_rounds(transcript)
    assert [r.user_text for r in rounds] == ["first question", "second question", "third question"]
    # the /exit + stdout plumbing folded into the third round, not new rounds
    assert len(rounds) == 3
    assert "c1" in rounds[-1].uuids and "c2" in rounds[-1].uuids
    assert rounds[0].assistant_text == "first answer"


def test_trim_one_round_is_resumable(transcript):
    plan, bak = edit.trim_rounds(transcript, 1)
    assert plan.rounds_kept == 2
    assert bak is not None and bak.exists()

    objs = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    uuids = {o.get("uuid") for o in objs}
    # the whole third round (incl. folded command plumbing) is gone
    for gone in ("u3", "a3", "c1", "c2"):
        assert gone not in uuids
    # the first two rounds survive intact
    for kept in ("u1", "a1", "r1", "a1b", "u2", "a2", "r2", "a2b"):
        assert kept in uuids
    # no dangling tool_use: every tool_use has its tool_result
    chain = edit._active_chain(edit._load(transcript))
    tu, tr = set(), set()
    for o in chain:
        for b in o.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tu.add(b["id"])
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tr.add(b["tool_use_id"])
    assert tu <= tr
    assert chain[-1]["uuid"] == "a2b"


def test_trim_two_rounds(transcript):
    plan, _ = edit.trim_rounds(transcript, 2)
    assert plan.rounds_kept == 1
    rounds = edit.list_rounds(transcript)
    assert [r.user_text for r in rounds] == ["first question"]


def test_non_message_lines_are_preserved(transcript):
    edit.trim_rounds(transcript, 1, backup=False)
    objs = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    assert any(o.get("type") == "file-history-snapshot" for o in objs)


def test_dry_run_writes_nothing(transcript):
    before = transcript.read_text()
    plan, bak = edit.trim_rounds(transcript, 1, dry_run=True)
    assert bak is None
    assert transcript.read_text() == before
    assert plan.rounds_removed == 1 and plan.lines_after < plan.lines_before


def test_no_backup_flag(transcript):
    _, bak = edit.trim_rounds(transcript, 1, backup=False)
    assert bak is None
    assert not list(transcript.parent.glob("*.bak-*"))


def test_trim_all_rounds_refused(transcript):
    with pytest.raises(edit.TrimError):
        edit.plan_trim_rounds(transcript, 3)
    with pytest.raises(edit.TrimError):
        edit.plan_trim_rounds(transcript, 99)


def test_trim_zero_or_negative_refused(transcript):
    with pytest.raises(edit.TrimError):
        edit.plan_trim_rounds(transcript, 0)
    with pytest.raises(edit.TrimError):
        edit.plan_trim_rounds(transcript, -1)


def test_empty_transcript_refused(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("")
    with pytest.raises(edit.TrimError):
        edit.plan_trim_rounds(f, 1)


def test_branch_only_active_chain_trimmed(tmp_path):
    """A dead branch (the user edited/branched) must be left untouched; only the
    active chain to the real leaf is considered."""
    f = tmp_path / "branch.jsonl"
    lines = [
        _user("u1", None, "q1"),
        _assistant_text("a1", "u1", "a1"),
        # dead branch off u1 (abandoned)
        _user("dead", "a1", "abandoned question"),
        _assistant_text("deadA", "dead", "abandoned answer"),
        # the real continuation (leaf) also hangs off a1
        _user("u2", "a1", "q2"),
        _assistant_text("a2", "u2", "a2 real leaf"),
    ]
    f.write_text("".join(_line(o) for o in lines))
    rounds = edit.list_rounds(f)
    # active chain leaf is a2 → chain is u1,a1,u2,a2 → 2 rounds (dead branch excluded)
    assert [r.user_text for r in rounds] == ["q1", "q2"]
    edit.trim_rounds(f, 1, backup=False)
    uuids = {json.loads(l)["uuid"] for l in f.read_text().splitlines() if l.strip()}
    assert "u2" not in uuids and "a2" not in uuids       # trimmed
    assert {"u1", "a1", "dead", "deadA"} <= uuids         # dead branch untouched
