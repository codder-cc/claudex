"""Phase 1 live-loop test using a STUB claude binary.

Proves the supervisor's spawn -> request -> SIGTERM -> relaunch(--resume) ->
natural-exit loop on tmp profiles with a fake claude. No real claude is run and
no credential is ever accessed.
"""

import importlib
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals / stub shebang")


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
    return tmp_path, profmod.ProfileManager()


def _make_stub(tmp_path: Path) -> Path:
    """A fake claude: logs each launch (config dir + args); exits 0 on the 2nd
    launch so the supervisor loop ends; otherwise waits to be SIGTERMed."""
    log = tmp_path / "stub.log"
    cnt = tmp_path / "stub.cnt"
    stub = tmp_path / "claude_stub.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time, pathlib\n"
        f"log = pathlib.Path({str(log)!r})\n"
        f"cnt = pathlib.Path({str(cnt)!r})\n"
        "n = (int(cnt.read_text()) if cnt.exists() else 0) + 1\n"
        "cnt.write_text(str(n))\n"
        "with log.open('a') as f:\n"
        "    f.write('LAUNCH n=%d cfg=%s args=%s\\n' % (n, os.environ.get('CLAUDE_CONFIG_DIR'), ' '.join(sys.argv[1:])))\n"
        "if n >= 2:\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    time.sleep(30)\n"
        "except BaseException:\n"
        "    pass\n"
        "sys.exit(143)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_live_loop_switch_then_exit(env, tmp_path):
    _, pm = env
    from claudex.core.switch import sanitize_project_path
    from claudex.core.supervisor import Supervisor, SwitchRequest

    cwd = "/Users/x/proj"
    sid = "33333333-3333-3333-3333-333333333333"
    # source profile with a transcript (has a thinking block to strip)
    pm.create("incr")
    src_cfg = pm.get("incr").config_dir
    pdir = src_cfg / "projects" / sanitize_project_path(cwd)
    pdir.mkdir(parents=True)
    (pdir / f"{sid}.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "q"}}),
        json.dumps({"type": "assistant", "uuid": "u", "parentUuid": None,
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "t", "signature": "s"},
                        {"type": "text", "text": "a"}]}}),
    ]) + "\n", encoding="utf-8")
    pm.create("mystic")
    dst_cfg = pm.get("mystic").config_dir

    stub = _make_stub(tmp_path)
    req_path = tmp_path / ".switch_request"
    sup_path = tmp_path / ".supervisor"

    sup = Supervisor(
        "incr",
        extra_args=[],
        pm=pm,
        request_path=req_path,
        supervisor_file=sup_path,
        claude_bin=str(stub),  # stub has a shebang + +x, so it runs directly
        install_hook=True,
        poll_seconds=0.1,
    )

    rc_holder = {}

    def runner():
        rc_holder["rc"] = sup.run()

    t = threading.Thread(target=runner)
    t.start()

    log = tmp_path / "stub.log"
    # wait for first launch
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (not log.exists() or "n=1" not in log.read_text()):
        time.sleep(0.05)
    assert log.exists() and "n=1" in log.read_text(), "stub never launched"

    # file a switch request (as the hook would)
    SwitchRequest(target="mystic", session_id=sid, cwd=cwd,
                  source_profile="incr", model=1).write(req_path)

    t.join(timeout=15)
    assert not t.is_alive(), "supervisor loop did not terminate"
    assert rc_holder.get("rc") == 0

    lines = log.read_text().splitlines()
    assert len(lines) == 2, lines
    # 1st launch under incr, no --resume
    assert str(src_cfg) in lines[0] and "--resume" not in lines[0]
    # 2nd launch under mystic, with --resume <sid>
    assert str(dst_cfg) in lines[1]
    assert "--resume" in lines[1] and sid in lines[1]

    # transcript carried into mystic (stripped), source intact
    carried = dst_cfg / "projects" / sanitize_project_path(cwd) / f"{sid}.jsonl"
    assert carried.is_file()
    for ln in carried.read_text().splitlines():
        if ln.strip():
            o = json.loads(ln)
            if o.get("type") == "assistant":
                assert all(b.get("type") != "thinking" for b in o["message"]["content"])
    src_text = (src_cfg / "projects" / sanitize_project_path(cwd) / f"{sid}.jsonl").read_text()
    assert "thinking" in src_text  # source untouched

    # hook installed into both profiles during the run, restored after exit
    from claudex.core.supervisor import HOOK_COMMAND
    for cfg in (src_cfg, dst_cfg):
        s = cfg / "settings.json"
        if s.exists():
            assert HOOK_COMMAND not in s.read_text()  # restored
    # liveness + request cleaned up
    assert not sup_path.exists()
    assert not req_path.exists()


def test_live_loop_natural_exit_no_switch(env, tmp_path):
    """No /switch: supervisor just hosts claude and returns its exit code."""
    _, pm = env
    from claudex.core.supervisor import Supervisor

    pm.create("incr")
    # stub that exits immediately (n>=2 branch not needed; make it exit on n=1)
    stub = tmp_path / "claude_exit.py"
    stub.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(7)\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    sup = Supervisor("incr", pm=pm,
                     request_path=tmp_path / ".switch_request",
                     supervisor_file=tmp_path / ".supervisor",
                     claude_bin=str(stub), install_hook=True, poll_seconds=0.1)
    rc = sup.run()
    assert rc == 7
    assert not (tmp_path / ".supervisor").exists()


def test_proxy_override_controls_base_url(env, monkeypatch):
    """proxy_override forces proxy mode on/off regardless of the stored flag."""
    from claudex.core.supervisor import Supervisor
    tmp_path, pm = env
    pm.create("incr")

    # Stub the shared daemon so the test never spawns a real proxy process.
    monkeypatch.setattr("claudex.core.proxy.ensure_shared",
                        lambda *a, **k: "http://127.0.0.1:59999")

    # override=True → env routed through the (stubbed) proxy + telemetry off
    sup = Supervisor("incr", pm=pm, proxy_override=True, install_hook=False)
    env2 = sup._env_for(str(pm.get("incr").config_dir), "incr")
    assert env2["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:59999"
    assert env2["DISABLE_TELEMETRY"] == "1"

    # override=False → no proxy env at all
    sup2 = Supervisor("incr", pm=pm, proxy_override=False, install_hook=False)
    env3 = sup2._env_for(str(pm.get("incr").config_dir), "incr")
    assert "ANTHROPIC_BASE_URL" not in env3
