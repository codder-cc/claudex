"""Tests for claudex.core.policy — local remote-settings override."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap

import pytest

from claudex.core import policy


# ---------------------------------------------------------------------------
# State round-trips
# ---------------------------------------------------------------------------

def test_default_policy_is_off(tmp_path):
    assert policy.get_policy("work", policy_dir=tmp_path) == {
        "mode": "off", "settings": None, "label": None}


def test_set_reject_and_read_back(tmp_path):
    policy.set_policy("work", "reject", policy_dir=tmp_path)
    assert policy.get_policy("work", policy_dir=tmp_path)["mode"] == "reject"
    # Persisted as a 0600 JSON file named after the profile.
    state = tmp_path / "work.json"
    assert state.exists()
    assert oct(state.stat().st_mode)[-3:] == "600"


def test_set_requires_dict_settings(tmp_path):
    with pytest.raises(ValueError):
        policy.set_policy("work", "set", settings=None, policy_dir=tmp_path)
    with pytest.raises(ValueError):
        policy.set_policy("work", "set", settings=["not", "a", "dict"], policy_dir=tmp_path)


def test_set_stores_settings(tmp_path):
    payload = {"permissions": {"defaultMode": "acceptEdits"}}
    policy.set_policy("work", "set", settings=payload, policy_dir=tmp_path)
    pol = policy.get_policy("work", policy_dir=tmp_path)
    assert pol["mode"] == "set"
    assert pol["settings"] == payload


def test_off_clears_state(tmp_path):
    policy.set_policy("work", "freeze", policy_dir=tmp_path)
    assert (tmp_path / "work.json").exists()
    policy.set_policy("work", "off", policy_dir=tmp_path)
    assert not (tmp_path / "work.json").exists()
    assert policy.get_policy("work", policy_dir=tmp_path)["mode"] == "off"


def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        policy.set_policy("work", "bogus", policy_dir=tmp_path)


def test_corrupt_state_reads_as_off(tmp_path):
    (tmp_path / "work.json").write_text("{not json", encoding="utf-8")
    assert policy.get_policy("work", policy_dir=tmp_path)["mode"] == "off"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_builtin_default_template_is_nonempty(tmp_path):
    t = policy.load_template("default", templates_dir=tmp_path)
    assert t  # non-empty, else it would collapse to "no managed settings"
    assert t["env"]["DISABLE_TELEMETRY"] == "1"


def test_list_templates_includes_builtins(tmp_path):
    names = policy.list_templates(templates_dir=tmp_path)
    assert "default" in names and names["default"] == "built-in"
    assert "private" in names


def test_custom_template_file_shadows_builtin(tmp_path):
    (tmp_path / "default.json").write_text('{"env": {"FOO": "bar"}}', encoding="utf-8")
    t = policy.load_template("default", templates_dir=tmp_path)
    assert t == {"env": {"FOO": "bar"}}
    assert policy.list_templates(templates_dir=tmp_path)["default"] != "built-in"


def test_unknown_template_raises(tmp_path):
    with pytest.raises(KeyError):
        policy.load_template("nope", templates_dir=tmp_path)


def test_apply_template_sets_labelled_policy(tmp_path):
    policy.apply_template("work", "default", policy_dir=tmp_path, templates_dir=tmp_path)
    pol = policy.get_policy("work", policy_dir=tmp_path)
    assert pol["mode"] == "set"
    assert pol["label"] == "template:default"
    assert pol["settings"]["env"]["DISABLE_TELEMETRY"] == "1"


def test_template_file_must_be_object(tmp_path):
    (tmp_path / "bad.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        policy.load_template("bad", templates_dir=tmp_path)


# ---------------------------------------------------------------------------
# Shim installation
# ---------------------------------------------------------------------------

def test_ensure_shim_writes_versioned_file(tmp_path):
    path = policy.ensure_shim(shim_dir=tmp_path)
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith(
        f"// claudex-policy-shim v{policy.SHIM_VERSION}")


def test_ensure_shim_rewrites_stale(tmp_path):
    path = policy.shim_path(shim_dir=tmp_path)
    tmp_path.mkdir(exist_ok=True)
    path.write_text("// claudex-policy-shim v0\nold", encoding="utf-8")
    policy.ensure_shim(shim_dir=tmp_path)
    assert path.read_text(encoding="utf-8").startswith(
        f"// claudex-policy-shim v{policy.SHIM_VERSION}")


# ---------------------------------------------------------------------------
# Env injection
# ---------------------------------------------------------------------------

def test_augment_env_noop_when_off(tmp_path):
    env = {"PATH": "/usr/bin"}
    out = policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim")
    assert out == {"PATH": "/usr/bin"}
    assert "NODE_OPTIONS" not in out


def test_augment_env_installs_shim_for_reject(tmp_path):
    policy.set_policy("work", "reject", policy_dir=tmp_path)
    env: dict = {}
    policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim",
                       logs_dir=tmp_path / "logs")
    assert "--require" in env["NODE_OPTIONS"]
    assert env["CLAUDEX_POLICY_FILE"] == str(tmp_path / "work.json")
    assert env["CLAUDEX_POLICY_LOG"] == str(tmp_path / "logs" / "work.log")
    assert (tmp_path / "shim" / policy.SHIM_NAME).exists()


def test_augment_env_logs_launch_and_injects_privacy_env(tmp_path):
    # A 'set' policy whose settings disable telemetry → claudex injects it directly.
    policy.set_policy("work", "set", {"env": {"DISABLE_TELEMETRY": "1"}},
                      policy_dir=tmp_path, label="template:default")
    env: dict = {}
    policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim",
                       logs_dir=tmp_path / "logs")
    # Privacy env pushed straight into the launch env (eligibility-independent).
    assert env["DISABLE_TELEMETRY"] == "1"
    # Launch recorded in the activity log.
    recs = policy.read_log("work", logs_dir=tmp_path / "logs")
    assert recs and recs[-1]["event"] == "launch"
    assert recs[-1]["injected_env"] == {"DISABLE_TELEMETRY": "1"}
    assert recs[-1]["mode"] == "set"


def test_read_log_empty_and_clear(tmp_path):
    assert policy.read_log("work", logs_dir=tmp_path) == []
    policy.append_log("work", {"event": "x"}, logs_dir=tmp_path)
    assert len(policy.read_log("work", logs_dir=tmp_path)) == 1
    policy.clear_log("work", logs_dir=tmp_path)
    assert policy.read_log("work", logs_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# Direct remote-settings.json control (the cached-file delivery model)
# ---------------------------------------------------------------------------

def _org_remote(config_dir):
    """Simulate an org-pushed remote-settings.json with a deny list."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "remote-settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Edit(~/Documents/**)"]},
                    "env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}}),
        encoding="utf-8")


def test_apply_remote_file_reject_empties_and_backs_up(tmp_path):
    _org_remote(tmp_path)
    policy.apply_remote_file(tmp_path, "reject", None)
    assert json.loads((tmp_path / "remote-settings.json").read_text()) == {}
    # Original preserved for restore.
    assert (tmp_path / "remote-settings.json.claudex-bak").exists()


def test_apply_remote_file_set_replaces_deny(tmp_path):
    _org_remote(tmp_path)
    policy.apply_remote_file(tmp_path, "set", {"env": {"DISABLE_TELEMETRY": "1"}})
    data = json.loads((tmp_path / "remote-settings.json").read_text())
    assert "permissions" not in data          # deny gone
    assert data["env"] == {"DISABLE_TELEMETRY": "1"}


def test_apply_remote_file_freeze_leaves_untouched(tmp_path):
    _org_remote(tmp_path)
    before = (tmp_path / "remote-settings.json").read_text()
    assert policy.apply_remote_file(tmp_path, "freeze", None) is None
    assert (tmp_path / "remote-settings.json").read_text() == before


def test_restore_remote_file_recovers_org_policy(tmp_path):
    _org_remote(tmp_path)
    policy.apply_remote_file(tmp_path, "reject", None)
    assert policy.restore_remote_file(tmp_path) is True
    data = json.loads((tmp_path / "remote-settings.json").read_text())
    assert data["permissions"]["deny"] == ["Edit(~/Documents/**)"]
    assert not (tmp_path / "remote-settings.json.claudex-bak").exists()


def test_augment_env_rewrites_remote_file_and_logs(tmp_path):
    cfg = tmp_path / "cfg"
    _org_remote(cfg)
    policy.set_policy("work", "set", {"env": {"DISABLE_TELEMETRY": "1"}},
                      policy_dir=tmp_path, label="template:default")
    env: dict = {}
    policy.augment_env(env, "work", config_dir=cfg, policy_dir=tmp_path,
                       shim_dir=tmp_path / "shim", logs_dir=tmp_path / "logs")
    # remote file rewritten → no deny
    data = json.loads((cfg / "remote-settings.json").read_text())
    assert "permissions" not in data and data["env"]["DISABLE_TELEMETRY"] == "1"
    # logged the file action
    rec = policy.read_log("work", logs_dir=tmp_path / "logs")[-1]
    assert rec["remote_file"] and "remote-settings.json" in rec["remote_file"]


def test_augment_env_preserves_existing_node_options(tmp_path):
    policy.set_policy("work", "reject", policy_dir=tmp_path)
    env = {"NODE_OPTIONS": "--max-old-space-size=4096"}
    policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim")
    assert env["NODE_OPTIONS"].startswith("--max-old-space-size=4096 ")
    assert "--require" in env["NODE_OPTIONS"]


def test_augment_env_idempotent(tmp_path):
    policy.set_policy("work", "reject", policy_dir=tmp_path)
    env: dict = {}
    policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim")
    once = env["NODE_OPTIONS"]
    policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim")
    assert env["NODE_OPTIONS"] == once  # not doubled


# ---------------------------------------------------------------------------
# Node smoke test: the shim actually intercepts the endpoint.
# Skipped where node is unavailable.
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")


def _run_shim(tmp_path, mode, settings=None):
    """Load the shim under the given policy, then do an https.get to the target
    path and to an unrelated path; return the harness's JSON output."""
    policy.set_policy("p", mode, settings=settings, policy_dir=tmp_path)
    shim = policy.ensure_shim(shim_dir=tmp_path)
    harness = tmp_path / "harness.cjs"
    harness.write_text(textwrap.dedent("""
        'use strict';
        const https = require('https');
        function hit(path) {
          return new Promise((resolve) => {
            const req = https.request('https://api.anthropic.com' + path, (res) => {
              let body = '';
              res.on('data', (c) => { body += c; });
              res.on('end', () => resolve({ status: res.statusCode, body }));
            });
            req.on('error', (e) => resolve({ error: String(e && e.message) }));
            req.end();
          });
        }
        (async () => {
          const settings = await hit('/api/claude_code/settings');
          // Unrelated path on the SAME host must pass through to the real
          // server (real status) or fail to connect (error) — either way it
          // must NOT carry our synthetic status code.
          const other = await hit('/v1/does-not-exist-claudex-test');
          console.log(JSON.stringify({ settings, other }));
        })();
    """), encoding="utf-8")
    env = {
        **os.environ,
        "CLAUDEX_POLICY_FILE": str(tmp_path / "p.json"),
        "CLAUDEX_POLICY_LOG": str(tmp_path / "p.log"),
    }
    proc = subprocess.run(
        [_NODE, "--require", str(shim), str(harness)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    # The harness prints one JSON line on stdout.
    line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")][-1]
    return json.loads(line)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_shim_reject_returns_204(tmp_path):
    out = _run_shim(tmp_path, "reject")
    assert out["settings"]["status"] == 204
    # Unrelated path must NOT get our synthetic 204 — it passed through.
    assert out["other"].get("status") != 204


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_shim_set_returns_200_with_settings(tmp_path):
    payload = {"permissions": {"defaultMode": "acceptEdits"}}
    out = _run_shim(tmp_path, "set", settings=payload)
    assert out["settings"]["status"] == 200
    body = json.loads(out["settings"]["body"])
    assert body["settings"] == payload
    assert body["checksum"].startswith("sha256:")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_shim_freeze_returns_304(tmp_path):
    out = _run_shim(tmp_path, "freeze")
    assert out["settings"]["status"] == 304


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_shim_writes_intercept_log(tmp_path):
    _run_shim(tmp_path, "reject")
    logged = [json.loads(l) for l in
              (tmp_path / "p.log").read_text(encoding="utf-8").splitlines() if l.strip()]
    intercepts = [r for r in logged if r.get("event") == "settings_intercept"]
    assert intercepts and intercepts[-1]["status"] == 204
    assert intercepts[-1]["mode"] == "reject"
