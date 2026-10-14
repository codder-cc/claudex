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
    assert policy.get_policy("work", policy_dir=tmp_path) == {"mode": "off", "settings": None}


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
    policy.augment_env(env, "work", policy_dir=tmp_path, shim_dir=tmp_path / "shim")
    assert "--require" in env["NODE_OPTIONS"]
    assert env["CLAUDEX_POLICY_FILE"] == str(tmp_path / "work.json")
    assert (tmp_path / "shim" / policy.SHIM_NAME).exists()


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
    env = {**os.environ, "CLAUDEX_POLICY_FILE": str(tmp_path / "p.json")}
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
