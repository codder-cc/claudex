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


# ---------------------------------------------------------------------------
# Routing: a /switch reaches only the supervisor hosting its claude
# ---------------------------------------------------------------------------

def _switch_stdin(target="mystic"):
    return json.dumps({"prompt": f"/switch {target}", "session_id": "s", "cwd": "/c"})


def test_hook_routes_to_own_supervisor_only(env, monkeypatch):
    """Two tabs under `claudex run`: /switch in one must not touch the other
    (the bug that killed every supervised tab on a single switch)."""
    tmp_path, pm, seed = env
    pm.create("incr")
    pm.create("mystic")
    from claudex.constants import ENV_SUPERVISOR
    from claudex.core.supervisor import (
        Liveness, handle_switch_hook, liveness_path, request_path_for,
    )
    mine, other = os.getpid(), os.getppid()  # both alive
    Liveness(pid=mine, profile="incr").write(liveness_path(mine))
    Liveness(pid=other, profile="incr").write(liveness_path(other))
    monkeypatch.setenv(ENV_SUPERVISOR, str(mine))

    out, _ = handle_switch_hook(_switch_stdin(), pm=pm)
    assert "Switching" in json.loads(out)["reason"]
    assert request_path_for(mine).exists()
    assert not request_path_for(other).exists()


def test_hook_without_supervisor_blocks_and_writes_nothing(env, monkeypatch):
    """A claude started without `claudex run` (the hook sits in the profile's
    shared settings.json) is told so; running supervisors are left alone."""
    tmp_path, pm, seed = env
    pm.create("incr")
    pm.create("mystic")
    from claudex.constants import ENV_SUPERVISOR
    from claudex.core.supervisor import Liveness, handle_switch_hook, liveness_path
    Liveness(pid=os.getppid(), profile="incr").write(liveness_path(os.getppid()))
    monkeypatch.delenv(ENV_SUPERVISOR, raising=False)

    out, _ = handle_switch_hook(_switch_stdin(), pm=pm)
    assert "needs a claudex supervisor" in json.loads(out)["reason"]
    assert not list((tmp_path / "supervisors").glob("*.request.json"))


def test_hook_ignores_dead_supervisor(env, monkeypatch):
    tmp_path, pm, seed = env
    pm.create("incr")
    pm.create("mystic")
    from claudex.constants import ENV_SUPERVISOR
    from claudex.core.supervisor import (
        Liveness, handle_switch_hook, liveness_path, request_path_for,
    )
    dead = 2_000_000_000  # no such pid
    Liveness(pid=dead, profile="incr").write(liveness_path(dead))
    monkeypatch.setenv(ENV_SUPERVISOR, str(dead))

    out, _ = handle_switch_hook(_switch_stdin(), pm=pm)
    assert "needs a claudex supervisor" in json.loads(out)["reason"]
    assert not request_path_for(dead).exists()
    assert not liveness_path(dead).exists()  # pruned


def test_child_env_carries_supervisor_pid(env, monkeypatch):
    tmp_path, pm, seed = env
    pm.create("incr")
    from claudex.constants import ENV_SUPERVISOR
    from claudex.core.supervisor import Supervisor
    sup = Supervisor("incr", pm=pm, proxy_override=False, install_hook=False)
    env_ = sup._env_for(str(pm.get("incr").config_dir), "incr")
    assert env_[ENV_SUPERVISOR] == str(os.getpid())


@pytest.mark.skipif(os.name == "nt", reason="execvpe launch path")
def test_unsupervised_launch_drops_inherited_supervisor_pid(monkeypatch):
    from claudex.constants import ENV_SUPERVISOR
    from claudex.core.session import SessionManager
    seen = {}
    monkeypatch.setattr("claudex.core.session.os.execvpe",
                        lambda cmd0, cmd, env: seen.update(env=env))
    SessionManager.__new__(SessionManager)._launch(["claude"], {ENV_SUPERVISOR: "123", "A": "1"})
    assert seen["env"] == {"A": "1"}


# ---------------------------------------------------------------------------
# Hook lifecycle: shared settings.json, several supervisors
# ---------------------------------------------------------------------------

def test_shared_hook_stays_until_last_supervisor_exits(env):
    """The first tab to exit must not pull the hook from under a tab still
    running on the same profile; the last one out removes it."""
    tmp_path, pm, seed = env
    pm.create("incr")
    cfg = pm.get("incr").config_dir
    from claudex.core.supervisor import HOOK_COMMAND, Liveness, Supervisor, liveness_path
    sup = Supervisor("incr", pm=pm)
    sup._ensure_hook(cfg)
    sup._write_liveness()
    assert str(cfg) in Liveness.read(liveness_path(os.getpid())).hooks

    other = os.getppid()
    Liveness(pid=other, profile="incr", hooks=[str(cfg)]).write(liveness_path(other))
    sup._restore_hooks()
    assert HOOK_COMMAND in (cfg / "settings.json").read_text()

    # a supervisor finding the hook another live one owns shares it
    sup._ensure_hook(cfg)
    assert cfg in sup._hooked_dirs
    liveness_path(other).unlink()  # the other tab exited
    sup._restore_hooks()
    assert HOOK_COMMAND not in (cfg / "settings.json").read_text()


def test_user_installed_hook_is_not_claimed(env):
    tmp_path, pm, seed = env
    pm.create("incr")
    cfg = pm.get("incr").config_dir
    from claudex.core.supervisor import HOOK_COMMAND, Supervisor, ensure_hook
    assert ensure_hook(cfg)  # installed by hand, no supervisor around
    sup = Supervisor("incr", pm=pm)
    sup._ensure_hook(cfg)
    sup._restore_hooks()
    assert HOOK_COMMAND in (cfg / "settings.json").read_text()


def test_restore_hook_keeps_changes_made_while_installed(env):
    """claude writes settings.json while the hook is in (e.g. effort level);
    removing the hook must keep that. A legacy pre-hook backup is ignored."""
    tmp_path, pm, seed = env
    pm.create("incr")
    cfg = pm.get("incr").config_dir
    settings = cfg / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    (cfg / "settings.json.claudex-switch-bak").write_text('{"theme": "dark"}', encoding="utf-8")
    from claudex.core.supervisor import ensure_hook, restore_hook
    ensure_hook(cfg)
    data = json.loads(settings.read_text())
    data["modelSettings"] = {"opus": {"effortLevel": "xhigh"}}
    settings.write_text(json.dumps(data), encoding="utf-8")

    restore_hook(cfg)
    assert json.loads(settings.read_text()) == {
        "theme": "dark", "modelSettings": {"opus": {"effortLevel": "xhigh"}},
    }


def test_ensure_hook_leaves_unreadable_settings_alone(env):
    tmp_path, pm, seed = env
    pm.create("incr")
    cfg = pm.get("incr").config_dir
    (cfg / "settings.json").write_text("{broken", encoding="utf-8")
    from claudex.core.supervisor import ensure_hook
    assert ensure_hook(cfg) is False
    assert (cfg / "settings.json").read_text() == "{broken"
