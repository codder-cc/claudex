"""Phase 0 unit tests for the switch mechanics (claudex.core.switch).

Pure mechanics only: no process launch, no credential access. Uses tmp dirs;
the real-transcript check reads an incr/mystic/work transcript READ-ONLY and
writes the carried copy into a tmp target — the real profile dirs are never
written to.
"""

import json
import os
from pathlib import Path

import pytest

from claudex.core.switch import (
    MAX_SANITIZED_LENGTH,
    SwitchRequest,
    carry_transcript,
    find_project_dir,
    parse_switch_command,
    resolve_transcript,
    rewrite_transcript,
    sanitize_project_path,
    strip_signature_blocks_from_entry,
    target_project_dir,
)


# ── sanitize_project_path ──────────────────────────────────────────────────

def test_sanitize_basic():
    assert sanitize_project_path("/Users/foo/my-project") == "-Users-foo-my-project"


def test_sanitize_all_nonalnum_to_dash():
    assert sanitize_project_path("a:b/c.d_e") == "a-b-c-d-e"


def test_sanitize_long_path_truncates_to_cap():
    long = "/" + "x" * 500
    out = sanitize_project_path(long)
    assert len(out) == MAX_SANITIZED_LENGTH


# ── signature stripping ────────────────────────────────────────────────────

def _assistant(content):
    return {"type": "assistant", "uuid": "u1", "parentUuid": None,
            "message": {"role": "assistant", "content": content}}


def test_strip_removes_thinking_keeps_text_and_tool():
    entry = _assistant([
        {"type": "thinking", "thinking": "secret", "signature": "sig"},
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
    ])
    out, removed = strip_signature_blocks_from_entry(entry)
    assert removed == 1
    types = [b["type"] for b in out["message"]["content"]]
    assert types == ["text", "tool_use"]
    # original object untouched (no in-place mutation)
    assert len(entry["message"]["content"]) == 3


def test_strip_removes_redacted_and_connector():
    entry = _assistant([
        {"type": "redacted_thinking", "data": "x"},
        {"type": "connector_text", "text": "y", "signature": "s"},
        {"type": "text", "text": "keep"},
    ])
    out, removed = strip_signature_blocks_from_entry(entry)
    assert removed == 2
    assert [b["type"] for b in out["message"]["content"]] == ["text"]


def test_strip_thinking_only_becomes_empty_content_entry_kept():
    entry = _assistant([{"type": "thinking", "thinking": "t", "signature": "s"}])
    out, removed = strip_signature_blocks_from_entry(entry)
    assert removed == 1
    assert out["message"]["content"] == []  # entry kept, chain preserved


def test_strip_ignores_user_and_non_list():
    user = {"type": "user", "message": {"role": "user", "content": "hi"}}
    assert strip_signature_blocks_from_entry(user) == (user, 0)


# ── rewrite_transcript ─────────────────────────────────────────────────────

def test_rewrite_strips_and_preserves_other_lines(tmp_path):
    src = tmp_path / "src.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "q"}}),
        json.dumps(_assistant([
            {"type": "thinking", "thinking": "t", "signature": "s"},
            {"type": "text", "text": "a"},
        ])),
        json.dumps({"type": "file-history-snapshot", "foo": 1}),
        "not json at all",
        "",
    ]
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dst = tmp_path / "out" / "dst.jsonl"

    stats = rewrite_transcript(src, dst, strip=True)

    assert stats.blocks_stripped == 1
    assert stats.entries_stripped == 1
    assert stats.lines_passthrough == 1  # the "not json" line
    out_lines = [ln for ln in dst.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # user + assistant + snapshot + passthrough = 4 content lines
    assert len(out_lines) == 4
    assert "not json at all" in out_lines
    assistant = json.loads(out_lines[1])
    assert [b["type"] for b in assistant["message"]["content"]] == ["text"]
    if os.name != "nt":
        assert (dst.stat().st_mode & 0o777) == 0o600


def test_rewrite_no_strip_keeps_thinking(tmp_path):
    src = tmp_path / "src.jsonl"
    src.write_text(json.dumps(_assistant([
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "text", "text": "a"},
    ])) + "\n", encoding="utf-8")
    dst = tmp_path / "dst.jsonl"
    stats = rewrite_transcript(src, dst, strip=False)
    assert stats.blocks_stripped == 0
    assistant = json.loads(dst.read_text().strip())
    assert [b["type"] for b in assistant["message"]["content"]] == ["thinking", "text"]


# ── carry_transcript + resolve ─────────────────────────────────────────────

def _seed_profile(root: Path, name: str, cwd: str, session_id: str, content_lines):
    cfg = root / name
    pdir = cfg / "projects" / sanitize_project_path(cwd)
    pdir.mkdir(parents=True)
    (pdir / f"{session_id}.jsonl").write_text(
        "\n".join(content_lines) + "\n", encoding="utf-8"
    )
    return cfg


def test_carry_lands_where_claude_resolves(tmp_path):
    cwd = "/Users/x/proj"
    sid = "11111111-1111-1111-1111-111111111111"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "q"}}),
        json.dumps(_assistant([
            {"type": "thinking", "thinking": "t", "signature": "s"},
            {"type": "text", "text": "a"},
        ])),
    ]
    src_cfg = _seed_profile(tmp_path, "incr", cwd, sid, lines)
    dst_cfg = tmp_path / "mystic"
    dst_cfg.mkdir()

    result = carry_transcript(src_cfg, dst_cfg, sid, cwd, strip=True)

    # lands at the sanitized path under target
    assert result.target == dst_cfg / "projects" / sanitize_project_path(cwd) / f"{sid}.jsonl"
    assert result.target.is_file()
    # a fresh `claude --resume` under the target dir would find it
    assert resolve_transcript(dst_cfg, sid, cwd) == result.target
    # stripped in the copy
    assert result.stats.blocks_stripped == 1
    # SOURCE UNTOUCHED: still present and still has the thinking block
    assert resolve_transcript(src_cfg, sid, cwd) is not None
    src_assistant = json.loads(
        resolve_transcript(src_cfg, sid, cwd).read_text().splitlines()[1]
    )
    assert any(b["type"] == "thinking" for b in src_assistant["message"]["content"])


def test_carry_project_config_sets_trust_preserves_identity(tmp_path):
    from claudex.core.switch import carry_project_config
    cwd = "/Users/x/proj"
    src = tmp_path / "incr"
    src.mkdir()
    dst = tmp_path / "mystic"
    dst.mkdir()
    (src / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "a@x"},
        "projects": {cwd: {"hasTrustDialogAccepted": True,
                           "allowedTools": ["Bash(ls)"]}},
    }), encoding="utf-8")
    (dst / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "b@y"},
        "userID": "device-b",
        "projects": {cwd: {"hasTrustDialogAccepted": False,
                           "allowedTools": ["Read"]}},
    }), encoding="utf-8")

    assert carry_project_config(src, dst, cwd) is True

    out = json.loads((dst / ".claude.json").read_text())
    # identity preserved
    assert out["oauthAccount"]["emailAddress"] == "b@y"
    assert out["userID"] == "device-b"
    # trust now accepted, tools unioned
    assert out["projects"][cwd]["hasTrustDialogAccepted"] is True
    assert set(out["projects"][cwd]["allowedTools"]) == {"Read", "Bash(ls)"}
    if os.name != "nt":
        assert (dst / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_carry_project_config_bails_on_unparseable_target(tmp_path):
    from claudex.core.switch import carry_project_config
    src = tmp_path / "incr"
    src.mkdir()
    dst = tmp_path / "mystic"
    dst.mkdir()
    (dst / ".claude.json").write_text("{corrupt", encoding="utf-8")
    before = (dst / ".claude.json").read_text()
    assert carry_project_config(src, dst, "/c") is False
    assert (dst / ".claude.json").read_text() == before  # never clobbered


def test_carry_missing_source_raises(tmp_path):
    (tmp_path / "incr").mkdir()
    (tmp_path / "mystic").mkdir()
    with pytest.raises(FileNotFoundError):
        carry_transcript(tmp_path / "incr", tmp_path / "mystic", "deadbeef", "/Users/x/proj")


def test_find_project_dir_exact(tmp_path):
    cwd = "/Users/x/proj"
    cfg = _seed_profile(tmp_path, "p", cwd, "sid0", ["{}"])
    found = find_project_dir(cfg, cwd)
    assert found == cfg / "projects" / sanitize_project_path(cwd)


def test_target_reuses_existing_project_dir(tmp_path):
    cwd = "/Users/x/proj"
    cfg = _seed_profile(tmp_path, "p", cwd, "sid0", ["{}"])
    assert target_project_dir(cfg, cwd) == cfg / "projects" / sanitize_project_path(cwd)


# ── parse_switch_command ───────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("switch work", "work"),            # plain prompt (the real trigger)
    ("  switch   mystic  ", "mystic"),
    ("switch 3", "3"),
    ("switch 1,3,5", "1,3,5"),
    ("/switch work", "work"),           # leading slash tolerated
    ("hello world", None),
    ("switchfoo", None),
    ("switch", None),
    ("switch the variable names", None),  # multi-word -> not a switch
    ("please switch work", None),         # only when it leads the prompt
])
def test_parse_switch_command(prompt, expected):
    assert parse_switch_command(prompt) == expected


# ── SwitchRequest handoff ──────────────────────────────────────────────────

def test_switch_request_roundtrip(tmp_path):
    req = SwitchRequest(target="mystic", session_id="sid", cwd="/Users/x/proj",
                        source_profile="incr", transcript_path="/a/b.jsonl")
    path = tmp_path / ".switch_request"
    req.write(path)
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    back = SwitchRequest.read(path)
    assert back is not None
    assert back.target == "mystic"
    assert back.session_id == "sid"
    assert back.cwd == "/Users/x/proj"
    assert back.model == 1


def test_switch_request_read_missing_returns_none(tmp_path):
    assert SwitchRequest.read(tmp_path / "nope") is None


def test_switch_request_read_garbage_returns_none(tmp_path):
    p = tmp_path / ".switch_request"
    p.write_text("{not json", encoding="utf-8")
    assert SwitchRequest.read(p) is None


# ── real-transcript carry (READ-ONLY on real profile, write to tmp) ─────────

def _first_real_transcript():
    home = Path.home()
    for name in ("incr", "mystic", "work"):
        base = home / ".claudex" / "profiles" / name / "projects"
        if not base.is_dir():
            continue
        for pdir in sorted(base.iterdir()):
            if not pdir.is_dir():
                continue
            for f in sorted(pdir.glob("*.jsonl")):
                if f.stat().st_size > 0:
                    return name, pdir.name, f
    return None


@pytest.mark.skipif(_first_real_transcript() is None, reason="no real transcript available")
def test_real_transcript_carry_readonly(tmp_path):
    name, encoded, src_file = _first_real_transcript()
    sid = src_file.stem
    src_before = src_file.read_bytes()

    src_cfg = Path.home() / ".claudex" / "profiles" / name
    dst_cfg = tmp_path / "target"
    dst_cfg.mkdir()

    # Decode cwd: prefer the transcript's own cwd field; else the encoded name.
    cwd = None
    for line in src_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict) and isinstance(o.get("cwd"), str):
            cwd = o["cwd"]
            break
    if cwd is None:
        pytest.skip("transcript has no cwd field")

    result = carry_transcript(src_cfg, dst_cfg, sid, cwd, strip=True)

    # carried copy exists and resolves under the target config dir
    assert result.target.is_file()
    assert resolve_transcript(dst_cfg, sid, cwd) == result.target
    # copy is valid JSONL throughout
    for line in result.target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)
    # no signature block survives in the carried assistant messages
    for line in result.target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if isinstance(o, dict) and o.get("type") == "assistant":
            content = (o.get("message") or {}).get("content")
            if isinstance(content, list):
                assert all(
                    b.get("type") not in {"thinking", "redacted_thinking", "connector_text"}
                    for b in content if isinstance(b, dict)
                )
    # SOURCE BYTES UNCHANGED
    assert src_file.read_bytes() == src_before
