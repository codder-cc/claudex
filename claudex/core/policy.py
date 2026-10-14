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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME, IS_MACOS, IS_LINUX

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

POLICY_DIR = CLAUDEX_HOME / "policy"          # per-profile state files
SHIM_DIR = CLAUDEX_HOME / "shim"              # the Node preload shim lives here
TEMPLATES_DIR = POLICY_DIR / "templates"      # user-defined policy templates (*.json)
LOGS_DIR = POLICY_DIR / "logs"                # per-profile activity logs (*.log, JSONL)

#: Privacy-related env vars. When a policy's served settings carry these in their
#: ``env`` block, claudex also injects them straight into the launched process
#: env — so telemetry is suppressed reliably, independent of whether Claude Code
#: considers the account eligible for remote managed settings.
PRIVACY_ENV_VARS = ("DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")

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
SHIM_VERSION = 3
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
                "lock": bool(data.get("lock", False)),
            }
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"mode": "off", "settings": None, "label": None, "lock": False}


def set_policy(
    profile: str,
    mode: str,
    settings: Optional[dict] = None,
    policy_dir: Path = POLICY_DIR,
    label: Optional[str] = None,
    lock: bool = False,
) -> dict:
    """Persist the policy for *profile*. ``off`` clears the state file.

    For ``set``, *settings* must be a JSON object (the managed-settings payload
    Claude Code will apply). *label* is an optional tag shown by ``show`` (e.g.
    the template name). *lock* makes the written remote-settings.json immutable
    at launch. Returns the resulting policy dict.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown policy mode {mode!r}; expected one of {VALID_MODES}")
    if mode == "off":
        clear_policy(profile, policy_dir)
        return {"mode": "off", "settings": None, "label": None, "lock": False}
    if mode == "set":
        if not isinstance(settings, dict):
            raise ValueError("mode 'set' requires a settings object (dict)")
    else:
        settings = None

    policy_dir.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "mode": mode, "settings": settings,
               "label": label, "lock": bool(lock)}
    path = _state_path(profile, policy_dir)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"mode": mode, "settings": settings, "label": label, "lock": bool(lock)}


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
    lock: bool = False,
) -> dict:
    """Activate template *name* for *profile* (as a ``set`` policy, labelled)."""
    settings = load_template(name, templates_dir)
    return set_policy(profile, "set", settings, policy_dir,
                      label=f"template:{name}", lock=lock)


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
# Direct control of the on-disk managed-settings cache
# ---------------------------------------------------------------------------
#
# Newer Claude Code builds deliver the org policy as a synced file
# (``remote-settings.json`` + a ``remote-settings.json.synced-at`` sidecar) and
# read it straight from disk at launch *without* always re-fetching — so the
# network shim never sees a request to intercept. The robust override for that
# model is to rewrite the file itself: the applied managed layer IS this file.
# The shim stays as a second line of defence for the builds/paths that do fetch.

REMOTE_SETTINGS_NAME = "remote-settings.json"
REMOTE_SETTINGS_BACKUP = "remote-settings.json.claudex-bak"
# Epoch-ms sidecar Claude Code writes on each sync; bumping it to "now" marks the
# cache fresh so a background re-sync won't re-download and clobber our override.
REMOTE_SETTINGS_SYNCED_AT = "remote-settings.json.synced-at"


def remote_settings_path(config_dir: Path) -> Path:
    return config_dir / REMOTE_SETTINGS_NAME


def _set_immutable(path: Path, on: bool) -> bool:
    """Toggle the OS immutable flag on *path* (best-effort).

    macOS: ``chflags uchg/nouchg`` (works as the file's owner). Linux: ``chattr
    +i/-i`` (usually needs root — fails quietly otherwise). Returns True on
    success. Used by the ``--lock`` override so Claude Code's background re-sync
    cannot overwrite our remote-settings.json with the org policy.
    """
    import subprocess
    if not path.exists():
        return False
    try:
        if IS_MACOS:
            flag = "uchg" if on else "nouchg"
            r = subprocess.run(["chflags", flag, str(path)],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        if IS_LINUX:
            flag = "+i" if on else "-i"
            r = subprocess.run(["chattr", flag, str(path)],
                               capture_output=True, timeout=5)
            return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def is_immutable(path: Path) -> bool:
    """Best-effort check whether *path* carries the OS immutable flag (macOS)."""
    try:
        if IS_MACOS:
            st = path.stat()
            # UF_IMMUTABLE == 0x2, exposed via st_flags on macOS.
            return bool(getattr(st, "st_flags", 0) & 0x2)
    except OSError:
        pass
    return False


def _deny_count(obj: object) -> int:
    try:
        deny = (obj or {}).get("permissions", {}).get("deny", [])  # type: ignore[union-attr]
        return len(deny) if isinstance(deny, list) else 0
    except AttributeError:
        return 0


def read_remote_file(config_dir: Path) -> Optional[dict]:
    """Return the parsed on-disk remote-settings.json, or None if absent/unreadable."""
    try:
        data = json.loads(remote_settings_path(config_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def apply_remote_file(config_dir: Path, mode: str, settings: Optional[dict],
                      lock: bool = False) -> Optional[dict]:
    """Rewrite ``config_dir/remote-settings.json`` to match the active policy.

    Backs up the org-pushed file once (``*.claudex-bak``) before the first
    overwrite. ``reject`` → ``{}`` (no managed policy), ``set`` → the served
    settings, ``freeze`` → left untouched. When *lock* is set, the file is made
    OS-immutable afterwards so Claude Code's background re-sync can't overwrite
    it with the org policy. Returns a dict describing the action (for the
    activity log), or ``None`` if nothing was written.
    """
    remote = remote_settings_path(config_dir)
    backup = config_dir / REMOTE_SETTINGS_BACKUP
    if mode == "freeze":
        return None
    if mode == "reject":
        payload: dict = {}
    elif mode == "set":
        payload = settings or {}
    else:
        return None
    try:
        before = read_remote_file(config_dir)
        removed_deny = _deny_count(before)
        config_dir.mkdir(parents=True, exist_ok=True)
        # Clear any prior immutable flag so we (and the backup copy) can write.
        _set_immutable(remote, False)
        # Preserve the original org policy once, so `policy off` can restore it.
        if remote.exists() and not backup.exists():
            backup.write_bytes(remote.read_bytes())
        remote.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            remote.chmod(0o600)
        except OSError:
            pass
        # Mark the cache freshly synced so a background re-sync defers and does
        # not re-download the org policy over our file.
        synced_at = None
        try:
            synced_at = int(datetime.now(timezone.utc).timestamp() * 1000)
            (config_dir / REMOTE_SETTINGS_SYNCED_AT).write_text(str(synced_at), encoding="utf-8")
        except OSError:
            synced_at = None
        locked = _set_immutable(remote, True) if lock else False
        return {
            "wrote": True,
            "path": str(remote),
            "keys": sorted(payload.keys()),
            "removed_deny": removed_deny,
            "backed_up": backup.exists(),
            "synced_at": synced_at,
            "locked": locked,
        }
    except OSError:
        return None


def restore_remote_file(config_dir: Path) -> bool:
    """Restore the org-pushed ``remote-settings.json`` from the claudex backup.

    Returns True if a backup was restored. If no backup exists, removes any
    claudex-written file so the next server sync re-downloads the real policy.
    """
    remote = remote_settings_path(config_dir)
    backup = config_dir / REMOTE_SETTINGS_BACKUP
    # Clear any immutable lock so the file can be restored/removed.
    _set_immutable(remote, False)
    # Drop our freshness stamp so Claude Code re-syncs the authoritative policy.
    try:
        (config_dir / REMOTE_SETTINGS_SYNCED_AT).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        if backup.exists():
            remote.write_bytes(backup.read_bytes())
            backup.unlink()
            return True
        # No backup: drop our file so a fresh sync pulls the authoritative one.
        remote.unlink(missing_ok=True)
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Env injection (used by both launch paths)
# ---------------------------------------------------------------------------

def _require_flag(shim: Path) -> str:
    # Quote so a space in the path survives NODE_OPTIONS' whitespace splitting.
    return f'--require "{shim}"'


def log_path(profile: str, logs_dir: Path = LOGS_DIR) -> Path:
    return logs_dir / f"{profile}.log"


def append_log(profile: str, record: dict, logs_dir: Path = LOGS_DIR) -> None:
    """Append one JSONL record to the profile's policy activity log (best-effort)."""
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with log_path(profile, logs_dir).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def read_log(profile: str, lines: int = 50, logs_dir: Path = LOGS_DIR) -> list[dict]:
    """Return the last *lines* parsed JSONL records from the profile's log."""
    try:
        raw = log_path(profile, logs_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in raw[-lines:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def clear_log(profile: str, logs_dir: Path = LOGS_DIR) -> None:
    try:
        log_path(profile, logs_dir).unlink()
    except (FileNotFoundError, OSError):
        pass


def augment_env(
    env: dict,
    profile: str,
    config_dir: Optional[Path] = None,
    policy_dir: Path = POLICY_DIR,
    shim_dir: Path = SHIM_DIR,
    logs_dir: Path = LOGS_DIR,
) -> dict:
    """Augment *env* in place so the launched ``claude`` applies *profile*'s policy.

    No-op (env returned unchanged) when the profile's mode is ``off``. Otherwise:
      * installs the shim and appends ``--require <shim>`` to ``NODE_OPTIONS``
        (preserving anything already there);
      * points the shim at the profile's state file and a per-profile log file;
      * when *config_dir* is given, rewrites its ``remote-settings.json`` to the
        active policy (the reliable override for builds that read the cached file
        without re-fetching);
      * for ``set`` policies, injects any PRIVACY_ENV_VARS from the served
        settings straight into the launch env (telemetry off regardless of
        remote-settings eligibility);
      * records a ``launch`` entry in the activity log for later inspection.
    """
    pol = get_policy(profile, policy_dir)
    mode = pol.get("mode", "off")
    if mode not in ACTIVE_MODES:
        return env

    shim = ensure_shim(shim_dir)
    flag = _require_flag(shim)
    existing = env.get("NODE_OPTIONS", "")
    if flag not in existing:
        env["NODE_OPTIONS"] = f"{existing} {flag}".strip() if existing else flag
    env["CLAUDEX_POLICY_FILE"] = str(_state_path(profile, policy_dir))
    env["CLAUDEX_POLICY_LOG"] = str(log_path(profile, logs_dir))
    # Opt-in stderr tracing from the shim, mirroring the parent's env.
    if os.environ.get("CLAUDEX_POLICY_DEBUG") == "1":
        env["CLAUDEX_POLICY_DEBUG"] = "1"

    # Primary override for the cached-file delivery model: rewrite the on-disk
    # remote-settings.json so the managed layer read at launch is ours.
    file_action = None
    if config_dir is not None:
        file_action = apply_remote_file(Path(config_dir), mode, pol.get("settings"),
                                        lock=pol.get("lock", False))

    # Belt-and-suspenders privacy: push telemetry kill-switches directly into the
    # launch env (not only via the served managed settings, which only apply when
    # eligible). Only a small, known-safe allowlist is forwarded.
    injected: dict[str, str] = {}
    served_env = ((pol.get("settings") or {}).get("env") or {}) if mode == "set" else {}
    for key in PRIVACY_ENV_VARS:
        if key in served_env:
            env[key] = str(served_env[key])
            injected[key] = str(served_env[key])

    append_log(profile, {
        "event": "launch",
        "mode": mode,
        "label": pol.get("label"),
        "shim": str(shim),
        "remote_file": file_action,
        "injected_env": injected,
    }, logs_dir)
    return env


# ---------------------------------------------------------------------------
# The shim (Node CommonJS, loaded via --require before Claude Code's bundle)
# ---------------------------------------------------------------------------

_SHIM_SOURCE = r'''// claudex-policy-shim v3
'use strict';
//
// Intercepts ONLY `GET <host>/api/claude_code/settings` and answers it from a
// claudex per-profile state file, letting you reject or override Claude Code's
// remote managed settings locally. Every other request passes through. Any
// error here falls back to the original request (fail-open).
//
// When CLAUDEX_POLICY_LOG points at a file, every interception is appended there
// as a JSON line so you can confirm the override actually ran.
//
const http = require('http');
const https = require('https');
const fs = require('fs');
const crypto = require('crypto');
const { Readable } = require('stream');
const { EventEmitter } = require('events');

const STATE_FILE = process.env.CLAUDEX_POLICY_FILE;
const LOG_FILE = process.env.CLAUDEX_POLICY_LOG;
const DEBUG = process.env.CLAUDEX_POLICY_DEBUG === '1';
// Both the legacy and the 2.1.x managed-settings pull endpoints.
const TARGET_PATHS = ['/api/claude_code/settings', '/managed/settings'];
function isTargetPath(p) { return TARGET_PATHS.indexOf(p) !== -1; }

function log() {
  if (!DEBUG) return;
  try { console.error.apply(console, ['[claudex-policy]'].concat([].slice.call(arguments))); } catch (e) {}
}

function logRecord(obj) {
  if (!LOG_FILE) return;
  try {
    fs.appendFileSync(LOG_FILE, JSON.stringify(Object.assign({ ts: new Date().toISOString() }, obj)) + '\n');
  } catch (e) { log('log write failed:', e && e.message); }
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
      return { status: 204, message: 'No Content', headers: {}, mode: 'reject', servedKeys: [] };
    case 'freeze':
      // 304 → Claude Code keeps the remote-settings.json already on disk.
      return { status: 304, message: 'Not Modified', headers: {}, mode: 'freeze', servedKeys: [] };
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
        mode: 'set',
        servedKeys: Object.keys(settings),
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
      const reqPath = pathOf(args);
      if (plan && isTargetPath(reqPath) && methodOf(args) === 'GET') {
        log('intercept GET', reqPath, '->', plan.status);
        logRecord({
          event: 'settings_intercept',
          path: reqPath,
          mode: plan.mode,
          status: plan.status,
          served_keys: plan.servedKeys,
        });
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
