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


def _chain_ok(path):
    """Assert the active chain is unbroken and has no dangling tool_use."""
    chain = edit._active_chain(edit._load(path))
    # parent links form a single unbroken line
    for child, parent in zip(chain[1:], chain):
        assert child.get("parentUuid") == parent.get("uuid")
    tu, tr = set(), set()
    for o in chain:
        for b in o.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tu.add(b["id"])
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tr.add(b["tool_use_id"])
    assert tu <= tr
    return chain


def test_list_messages_flattens_active_chain(transcript):
    msgs = edit.list_messages(transcript)
    kinds = [(m.role, m.kind) for m in msgs]
    assert ("user", "text") in kinds
    assert ("assistant", "tool_use") in kinds
    assert ("user", "tool_result") in kinds
    # command plumbing shows up but is not editable
    cmds = [m for m in msgs if m.kind == "command"]
    assert cmds and all(not m.editable for m in cmds)
    # every text user/assistant message is editable
    assert all(m.editable for m in msgs if m.kind == "text")


def test_edit_message_text_keeps_chain(transcript):
    msgs = edit.list_messages(transcript)
    first_user = next(m for m in msgs if m.role == "user" and m.kind == "text")
    edit.edit_message_text(transcript, first_user.uuid, "EDITED QUESTION", backup=False)
    msgs2 = edit.list_messages(transcript)
    assert any(m.uuid == first_user.uuid and m.text == "EDITED QUESTION" for m in msgs2)
    _chain_ok(transcript)


def test_edit_nonexistent_message_raises(transcript):
    with pytest.raises(edit.TrimError):
        edit.edit_message_text(transcript, "no-such-uuid", "x")


def test_delete_plain_message_relinks(transcript):
    # delete the middle user text message "second question" (u2)
    edit.delete_message(transcript, "u2", backup=False)
    uuids = {json.loads(l).get("uuid") for l in transcript.read_text().splitlines() if l.strip()}
    assert "u2" not in uuids
    chain = _chain_ok(transcript)
    # a2 (second answer's tool) was re-parented onto u2's parent (a1b)
    assert any(o["uuid"] == "a2" and o["parentUuid"] == "a1b" for o in chain)


def test_delete_tool_use_removes_its_result(transcript):
    edit.delete_message(transcript, "a2", backup=False)   # a tool_use
    uuids = {json.loads(l).get("uuid") for l in transcript.read_text().splitlines() if l.strip()}
    assert "a2" not in uuids and "r2" not in uuids         # the result went too
    _chain_ok(transcript)


def test_delete_tool_result_removes_its_use(transcript):
    edit.delete_message(transcript, "r2", backup=False)
    uuids = {json.loads(l).get("uuid") for l in transcript.read_text().splitlines() if l.strip()}
    assert "r2" not in uuids and "a2" not in uuids
    _chain_ok(transcript)


def test_delete_leaf_message(transcript):
    # the active leaf is a3 (command plumbing c1/c2 are also leafmost); delete a3's round tail
    edit.delete_message(transcript, "a3", backup=False)
    uuids = {json.loads(l).get("uuid") for l in transcript.read_text().splitlines() if l.strip()}
    assert "a3" not in uuids
    _chain_ok(transcript)


def test_truncate_to_cuts_tail(transcript):
    edit.truncate_to(transcript, "a1b", backup=False)   # keep through first answer
    uuids = {json.loads(l).get("uuid") for l in transcript.read_text().splitlines() if l.strip()}
    assert {"u1", "a1", "r1", "a1b"} <= uuids
    for gone in ("u2", "a2", "r2", "a2b", "u3", "a3"):
        assert gone not in uuids
    chain = _chain_ok(transcript)
    assert chain[-1]["uuid"] == "a1b"


def test_read_since_tails_appends(tmp_path):
    f = tmp_path / "live.jsonl"
    f.write_text(_line(_user("u1", None, "hi")))
    msgs, off = edit.read_since(f, 0)
    assert len(msgs) == 1 and off == f.stat().st_size
    # append two more lines, then a partial (no newline)
    with open(f, "a") as fh:
        fh.write(_line(_assistant_text("a1", "u1", "hello")))
        fh.write('{"partial": true')      # half-written line, no newline
    msgs2, off2 = edit.read_since(f, off)
    assert [m["uuid"] for m in msgs2] == ["a1"]      # partial line not consumed
    assert off2 < f.stat().st_size


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


def test_api_error_messages_are_hidden_from_turns(tmp_path):
    """Synthetic 'API Error: …safeguards flagged…' assistant messages are
    classified as kind 'error' and hidden from the default turns view."""
    f = tmp_path / "err.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "e1", "parentUuid": "u1",
         "isApiErrorMessage": True, "error": "invalid_request",
         "message": {"role": "assistant", "model": "<synthetic>", "stop_reason": "refusal",
                     "content": [{"type": "text",
                                  "text": "API Error: Opus's safeguards flagged this message."}]}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "e1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "real reply"}]}},
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    all_msgs = edit.list_messages(f)
    assert next(m for m in all_msgs if m.uuid == "e1").kind == "error"
    turns = edit.list_messages(f, turns=True)
    assert [m.uuid for m in turns] == ["u1", "a1"]   # the error turn is hidden
