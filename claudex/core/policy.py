"""Local override of Claude Code's remote managed settings (``remote-settings.json``).

Claude Code fetches enterprise "managed settings" from the Anthropic API on
startup and once an hour, and applies them as the top-priority, read-only
``policySettings`` layer — you cannot soften them with user/project settings.
This module lets you, the admin of your own profiles, intercept that one
endpoint locally and decide what Claude Code sees, per profile:

  * ``reject`` — endpoint answers ``204``; Claude Code treats it as "no managed
                 settings", drops its cache file, and the policy layer falls
                 through to nothing (fully local configuration).
  * ``freeze`` — endpoint answers ``304``; Claude Code keeps whatever
                 ``remote-settings.json`` is already on disk (no refetch churn).
  * ``set``    — endpoint answers ``200`` with YOUR settings JSON; Claude Code
                 validates it against its own schema and applies it as the
                 policy layer.
  * ``off``    — no interception; Claude Code talks to the real backend as usual.

Mechanism: a tiny Node preload shim (``--require``, injected via ``NODE_OPTIONS``)
patches ``http(s).request`` to answer ONLY ``GET /api/claude_code/settings`` from
a per-profile state file. Every other request — including the real inference API
on the same host — passes straight through. The shim fails *open*: on any error
it delegates to the original request, so Claude Code behaves exactly as it would
without claudex. Nothing here reads, writes or refreshes a credential.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

POLICY_DIR = CLAUDEX_HOME / "policy"          # per-profile state files
SHIM_DIR = CLAUDEX_HOME / "shim"              # the Node preload shim lives here
TEMPLATES_DIR = POLICY_DIR / "templates"      # user-defined policy templates (*.json)

#: Built-in policy templates — ready-made managed-settings payloads you can drop
#: in with `claudex policy default` / `--template` instead of hand-writing JSON.
#:
#: They must be NON-EMPTY: Claude Code treats an empty managed-settings object as
#: "no managed settings" (it clears the cache), so an empty template would behave
#: like `reject` rather than assert a policy layer. The `default` baseline imposes
#: no permission restrictions and turns telemetry off via the managed `env` block
#: (applied to process.env with top priority), so it cleanly supersedes a
#: restrictive/ telemetry-heavy remote policy with a permissive local one.
BUILTIN_TEMPLATES: dict[str, dict] = {
    "default": {
        "env": {"DISABLE_TELEMETRY": "1"},
    },
    "private": {
        # Harder privacy stance: also suppress all nonessential network traffic
        # (auto-update, release notes, model-capabilities probes, …).
        "env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    },
}

#: Bump when the shim source below changes so ``ensure_shim`` rewrites it.
SHIM_VERSION = 1
SHIM_NAME = "remote-settings-shim.cjs"

VALID_MODES = ("off", "reject", "freeze", "set")
ACTIVE_MODES = ("reject", "freeze", "set")    # modes that install the shim

# The endpoint the shim intercepts. Matched by pathname only, so it works
# regardless of the configured first-party base URL.
_ENDPOINT_PATH = "/api/claude_code/settings"


# ---------------------------------------------------------------------------
# State (per profile)
# ---------------------------------------------------------------------------

def _state_path(profile: str, policy_dir: Path = POLICY_DIR) -> Path:
    return policy_dir / f"{profile}.json"


def get_policy(profile: str, policy_dir: Path = POLICY_DIR) -> dict:
    """Return the stored policy for *profile* as ``{"mode", "settings", "label"}``.

    Missing or unreadable state is reported as mode ``off``. ``label`` is an
    optional human tag (e.g. a template name) or ``None``.
    """
    try:
        data = json.loads(_state_path(profile, policy_dir).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("mode") in VALID_MODES:
            return {
                "mode": data["mode"],
                "settings": data.get("settings"),
                "label": data.get("label"),
            }
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"mode": "off", "settings": None, "label": None}


def set_policy(
    profile: str,
    mode: str,
    settings: Optional[dict] = None,
    policy_dir: Path = POLICY_DIR,
    label: Optional[str] = None,
) -> dict:
    """Persist the policy for *profile*. ``off`` clears the state file.

    For ``set``, *settings* must be a JSON object (the managed-settings payload
    Claude Code will apply). *label* is an optional tag shown by ``show`` (e.g.
    the template name). Returns the resulting policy dict.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown policy mode {mode!r}; expected one of {VALID_MODES}")
    if mode == "off":
        clear_policy(profile, policy_dir)
        return {"mode": "off", "settings": None, "label": None}
    if mode == "set":
        if not isinstance(settings, dict):
            raise ValueError("mode 'set' requires a settings object (dict)")
    else:
        settings = None

    policy_dir.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "mode": mode, "settings": settings, "label": label}
    path = _state_path(profile, policy_dir)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"mode": mode, "settings": settings, "label": label}


# ---------------------------------------------------------------------------
# Templates — ready-made managed-settings payloads
# ---------------------------------------------------------------------------

def list_templates(templates_dir: Path = TEMPLATES_DIR) -> dict[str, str]:
    """Return available template names → source ('built-in' or a file path)."""
    out: dict[str, str] = {name: "built-in" for name in BUILTIN_TEMPLATES}
    try:
        for f in sorted(templates_dir.glob("*.json")):
            out[f.stem] = str(f)  # a file shadows a built-in of the same name
    except OSError:
        pass
    return out


def load_template(name: str, templates_dir: Path = TEMPLATES_DIR) -> dict:
    """Load a template's settings object by name.

    A ``<name>.json`` file in *templates_dir* takes precedence over a built-in,
    letting you override or extend the baseline. Raises ``KeyError``/``ValueError``
    if the name is unknown or the file is not a JSON object.
    """
    f = templates_dir / f"{name}.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"template file {f} must contain a JSON object")
        return data
    if name in BUILTIN_TEMPLATES:
        # Return a copy so callers can't mutate the module-level dict.
        return json.loads(json.dumps(BUILTIN_TEMPLATES[name]))
    raise KeyError(name)


def apply_template(
    profile: str,
    name: str,
    policy_dir: Path = POLICY_DIR,
    templates_dir: Path = TEMPLATES_DIR,
) -> dict:
    """Activate template *name* for *profile* (as a ``set`` policy, labelled)."""
    settings = load_template(name, templates_dir)
    return set_policy(profile, "set", settings, policy_dir, label=f"template:{name}")


def clear_policy(profile: str, policy_dir: Path = POLICY_DIR) -> None:
    """Remove the policy state for *profile* (back to ``off``)."""
    try:
        _state_path(profile, policy_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Shim installation
# ---------------------------------------------------------------------------

def shim_path(shim_dir: Path = SHIM_DIR) -> Path:
    return shim_dir / SHIM_NAME


def ensure_shim(shim_dir: Path = SHIM_DIR) -> Path:
    """Write the Node preload shim to disk if missing/outdated; return its path.

    The first line carries a version marker so a newer claudex rewrites an old
    shim in place.
    """
    path = shim_path(shim_dir)
    marker = f"// claudex-policy-shim v{SHIM_VERSION}"
    try:
        if path.exists() and path.read_text(encoding="utf-8").startswith(marker):
            return path
    except OSError:
        pass
    shim_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(_SHIM_SOURCE, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# Env injection (used by both launch paths)
# ---------------------------------------------------------------------------

def _require_flag(shim: Path) -> str:
    # Quote so a space in the path survives NODE_OPTIONS' whitespace splitting.
    return f'--require "{shim}"'


def augment_env(
    env: dict,
    profile: str,
    policy_dir: Path = POLICY_DIR,
    shim_dir: Path = SHIM_DIR,
) -> dict:
    """Augment *env* in place so the launched ``claude`` applies *profile*'s policy.

    No-op (env returned unchanged) when the profile's mode is ``off``. Otherwise
    installs the shim, appends ``--require <shim>`` to ``NODE_OPTIONS`` (preserving
    anything already there), and points the shim at the profile's state file.
    """
    mode = get_policy(profile, policy_dir).get("mode", "off")
    if mode not in ACTIVE_MODES:
        return env

    flag = _require_flag(ensure_shim(shim_dir))
    existing = env.get("NODE_OPTIONS", "")
    if flag not in existing:
        env["NODE_OPTIONS"] = f"{existing} {flag}".strip() if existing else flag
    env["CLAUDEX_POLICY_FILE"] = str(_state_path(profile, policy_dir))
    # Opt-in stderr tracing from the shim, mirroring the parent's env.
    if os.environ.get("CLAUDEX_POLICY_DEBUG") == "1":
        env["CLAUDEX_POLICY_DEBUG"] = "1"
    return env


# ---------------------------------------------------------------------------
# The shim (Node CommonJS, loaded via --require before Claude Code's bundle)
# ---------------------------------------------------------------------------

_SHIM_SOURCE = r'''// claudex-policy-shim v1
'use strict';
//
// Intercepts ONLY `GET <host>/api/claude_code/settings` and answers it from a
// claudex per-profile state file, letting you reject or override Claude Code's
// remote managed settings locally. Every other request passes through. Any
// error here falls back to the original request (fail-open).
//
const http = require('http');
const https = require('https');
const fs = require('fs');
const crypto = require('crypto');
const { Readable } = require('stream');
const { EventEmitter } = require('events');

const STATE_FILE = process.env.CLAUDEX_POLICY_FILE;
const DEBUG = process.env.CLAUDEX_POLICY_DEBUG === '1';
const TARGET_PATH = '/api/claude_code/settings';

function log() {
  if (!DEBUG) return;
  try { console.error.apply(console, ['[claudex-policy]'].concat([].slice.call(arguments))); } catch (e) {}
}

function readState() {
  try {
    const s = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    if (s && typeof s === 'object') return s;
  } catch (e) { log('state read failed:', e && e.message); }
  return null;
}

function sortKeysDeep(o) {
  if (Array.isArray(o)) return o.map(sortKeysDeep);
  if (o && typeof o === 'object') {
    const r = {};
    Object.keys(o).sort().forEach(function (k) { r[k] = sortKeysDeep(o[k]); });
    return r;
  }
  return o;
}

function checksumOf(settings) {
  const normalized = JSON.stringify(sortKeysDeep(settings));
  return 'sha256:' + crypto.createHash('sha256').update(normalized).digest('hex');
}

// Returns the synthetic response to send, or null to pass the request through.
function planResponse() {
  if (!STATE_FILE) return null;
  const st = readState();
  if (!st) return null;
  switch (st.mode) {
    case 'reject':
      // 204 → Claude Code treats it as "no managed settings" and clears cache.
      return { status: 204, message: 'No Content', headers: {} };
    case 'freeze':
      // 304 → Claude Code keeps the remote-settings.json already on disk.
      return { status: 304, message: 'Not Modified', headers: {} };
    case 'set': {
      const settings = (st.settings && typeof st.settings === 'object') ? st.settings : {};
      const body = JSON.stringify({
        uuid: 'claudex-local',
        checksum: checksumOf(settings),
        settings: settings,
      });
      return {
        status: 200,
        message: 'OK',
        body: body,
        headers: { 'content-type': 'application/json' },
      };
    }
    default:
      return null;
  }
}

function pathOf(args) {
  try {
    const a0 = args[0];
    if (typeof a0 === 'string') return new URL(a0).pathname;
    if (a0 && typeof a0.href === 'string' && typeof a0.pathname === 'string') return a0.pathname;
    if (a0 && typeof a0 === 'object') {
      if (typeof a0.pathname === 'string') return a0.pathname;
      if (typeof a0.path === 'string') return a0.path.split('?')[0];
    }
  } catch (e) { log('path parse failed:', e && e.message); }
  return null;
}

function methodOf(args) {
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a && typeof a === 'object' && typeof a.method === 'string') return a.method.toUpperCase();
  }
  return 'GET';
}

function callbackOf(args) {
  const last = args[args.length - 1];
  return typeof last === 'function' ? last : null;
}

// Minimal stand-in for http.ClientRequest: enough for axios / follow-redirects.
class FakeClientRequest extends EventEmitter {
  constructor(resp, cb) {
    super();
    this._resp = resp;
    if (cb) this.once('response', cb);
  }
  setHeader() {} getHeader() {} removeHeader() {}
  setNoDelay() {} setSocketKeepAlive() {} flushHeaders() {}
  setTimeout() { return this; }
  write(_chunk, enc, cb) {
    const done = typeof enc === 'function' ? enc : cb;
    if (typeof done === 'function') done();
    return true;
  }
  end(chunk, enc, cb) {
    const done = typeof chunk === 'function' ? chunk
               : typeof enc === 'function' ? enc
               : cb;
    const r = this._resp;
    const self = this;
    process.nextTick(function () {
      const res = new Readable({ read() {} });
      res.statusCode = r.status;
      res.statusMessage = r.message || '';
      res.headers = r.headers || {};
      res.rawHeaders = [];
      res.httpVersion = '1.1';
      res.complete = true;
      self.emit('response', res);
      process.nextTick(function () {
        if (r.body) res.push(r.body);
        res.push(null);
      });
    });
    if (typeof done === 'function') done();
    return this;
  }
  abort() {}
  destroy() { return this; }
}

function makeWrapper(original, autoEnd) {
  return function () {
    const args = [].slice.call(arguments);
    try {
      const plan = planResponse();
      if (plan && pathOf(args) === TARGET_PATH && methodOf(args) === 'GET') {
        log('intercept GET', TARGET_PATH, '->', plan.status);
        const req = new FakeClientRequest(plan, callbackOf(args));
        if (autoEnd) process.nextTick(function () { req.end(); });
        return req;
      }
    } catch (e) {
      log('wrapper error, passing through:', e && e.message);
    }
    return original.apply(this, args);
  };
}

[http, https].forEach(function (mod) {
  const origRequest = mod.request;
  const origGet = mod.get;
  mod.request = makeWrapper(origRequest, false);
  mod.get = makeWrapper(origGet, true);
});

log('installed; state file =', STATE_FILE);
'''
