"""Phase 0 end-to-end (dry-run) test of hook -> request -> supervisor -> plan.

Runs entirely on tmp profiles (monkeypatched constants). No claude launch, no
credential access, no writes outside tmp.
"""

import importlib
import json
import os

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("claudex.constants.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr("claudex.constants.SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr("claudex.constants.CLAUDEX_HOME", tmp_path)
    monkeypatch.setattr("claudex.constants.ACTIVE_PROFILE_FILE", tmp_path / ".active_profile")
    monkeypatch.setattr("claudex.constants.CURRENT_ENV_BASH", tmp_path / ".current_env")
    monkeypatch.setattr("claudex.constants.CURRENT_ENV_PWSH", tmp_path / ".current_env.ps1")
    import claudex.core.profile as profmod
    importlib.reload(profmod)
    pm = profmod.ProfileManager()

    from claudex.core.switch import sanitize_project_path

    def seed(name, cwd, sid, lines):
        pm.create(name)
        cfg = pm.get(name).config_dir
        pdir = cfg / "projects" / sanitize_project_path(cwd)
        pdir.mkdir(parents=True)
        (pdir / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cfg

    return tmp_path, pm, seed


def _assistant_with_thinking():
    return json.dumps({
        "type": "assistant", "uuid": "u1", "parentUuid": None,
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "text", "text": "hi"},
        ]},
    })


def test_end_to_end_dry_run(env):
    tmp_path, pm, seed = env
    cwd = "/Users/x/proj"
    sid = "22222222-2222-2222-2222-222222222222"
    src_cfg = seed("incr", cwd, sid, [
        json.dumps({"type": "user", "message": {"role": "user", "content": "q"}}),
        _assistant_with_thinking(),
    ])
    pm.create("mystic")
    dst_cfg = pm.get("mystic").config_dir

    from claudex.core.supervisor import Liveness, Supervisor, handle_switch_hook

    req_path = tmp_path / ".switch_request"
    sup_path = tmp_path / ".supervisor"

    # A live supervisor exists (use our own pid so is_pid_alive == True).
    Liveness(pid=os.getpid(), profile="incr", model=1).write(sup_path)

    # 1) Hook fires for a `/switch mystic` prompt.
    stdin = json.dumps({
        "prompt": "/switch mystic",
        "session_id": sid,
        "cwd": cwd,
        "transcript_path": str(src_cfg / "projects" / "x" / f"{sid}.jsonl"),
        "hook_event_name": "UserPromptSubmit",
    })
    out, code = handle_switch_hook(stdin, pm=pm, request_path=req_path, supervisor_file=sup_path)
    assert code == 0
    assert json.loads(out)["decision"] == "block"
    assert req_path.exists()  # request handed off

    # 2) Supervisor consumes it and produces a launch plan (no spawn/kill).
    sup = Supervisor("incr", dry_run=True, pm=pm,
                     request_path=req_path, supervisor_file=sup_path)
    plan = sup.run_dry(timeout=2)

    assert plan is not None
    assert plan.target == "mystic"
    assert plan.resumed_session == sid          # resolvable -> will --resume
    assert "--resume" in plan.cmd and sid in plan.cmd
    assert plan.env_config_dir == str(dst_cfg)
    assert plan.carried_from is not None        # transcript was carried
    assert plan.carry_blocks_stripped == 1      # thinking block stripped

    # carried copy exists under target and resolves; source untouched
    from claudex.core.switch import resolve_transcript
    carried = resolve_transcript(dst_cfg, sid, cwd)
    assert carried is not None and carried.is_file()
    for line in carried.read_text().splitlines():
        if line.strip():
            o = json.loads(line)
            if o.get("type") == "assistant":
                assert all(b.get("type") != "thinking"
                           for b in o["message"]["content"])
    # source still has its thinking block
    src = resolve_transcript(src_cfg, sid, cwd)
    assert any("thinking" in ln for ln in src.read_text().splitlines())

    # request consumed; liveness cleaned up; active advanced to target
    assert not req_path.exists()
    assert not sup_path.exists()


def test_hook_unknown_profile_blocks_without_request(env):
    tmp_path, pm, seed = env
    pm.create("incr")
    from claudex.core.supervisor import Liveness, handle_switch_hook
    req_path = tmp_path / ".switch_request"
    sup_path = tmp_path / ".supervisor"
    Liveness(pid=os.getpid(), profile="incr", model=1).write(sup_path)

    out, code = handle_switch_hook(
        json.dumps({"prompt": "/switch ghost", "session_id": "s", "cwd": "/c"}),
        pm=pm, request_path=req_path, supervisor_file=sup_path,
    )
    assert code == 0
    assert "Unknown profile" in json.loads(out)["reason"]
    assert not req_path.exists()


def test_round_trip_preserves_later_turns(env):
    """A→B→A must resume A with B's newer turns, not A's stale copy (the
    round-trip loss bug)."""
    tmp_path, pm, seed = env
    cwd = "/Users/x/proj"
    sid = "44444444-4444-4444-4444-444444444444"

    def line(text, role="user"):
        return json.dumps({"type": role, "message": {"role": role, "content": text}})

    a_cfg = seed("incr", cwd, sid, [line("turn1")])
    pm.create("mystic")
    b_cfg = pm.get("mystic").config_dir

    from claudex.core.switch import SwitchRequest, sanitize_project_path
    from claudex.core.supervisor import Supervisor

    sup = Supervisor("incr", pm=pm,
                     request_path=tmp_path / ".req", supervisor_file=tmp_path / ".sup")

    # 1) A -> B : carries [turn1] into B
    sup.prepare_target(SwitchRequest(target="mystic", session_id=sid, cwd=cwd,
                                     source_profile="incr", model=1))
    b_file = b_cfg / "projects" / sanitize_project_path(cwd) / f"{sid}.jsonl"
    assert b_file.is_file()

    # 2) B appends a turn (as `claude --resume` would while running under B)
    with b_file.open("a", encoding="utf-8") as f:
        f.write(line("turn2_on_B") + "\n")

    # 3) B -> A : must OVERWRITE A's stale [turn1] with B's [turn1, turn2_on_B]
    plan = sup.prepare_target(SwitchRequest(target="incr", session_id=sid, cwd=cwd,
                                            source_profile="mystic", model=1))
    assert plan.resumed_session == sid
    a_file = a_cfg / "projects" / sanitize_project_path(cwd) / f"{sid}.jsonl"
    text = a_file.read_text()
    assert "turn2_on_B" in text, "round-trip lost the turn written on B"
    assert "turn1" in text


def test_hook_already_on_target_blocks(env):
    tmp_path, pm, seed = env
    pm.create("incr")
    from claudex.core.supervisor import Liveness, handle_switch_hook
    req_path = tmp_path / ".switch_request"
    sup_path = tmp_path / ".supervisor"
    Liveness(pid=os.getpid(), profile="incr", model=1).write(sup_path)

    out, code = handle_switch_hook(
        json.dumps({"prompt": "/switch incr", "session_id": "s", "cwd": "/c"}),
        pm=pm, request_path=req_path, supervisor_file=sup_path,
    )
    assert "Already on" in json.loads(out)["reason"]
    assert not req_path.exists()
