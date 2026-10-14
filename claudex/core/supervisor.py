"""Supervisor + UserPromptSubmit hook handler for in-session profile switching.

Safety contract for the current phase:
  * The ONLY methods that spawn or signal an OS process are
    :meth:`Supervisor._spawn_claude` and :meth:`Supervisor._terminate_child`,
    and they are never reached when ``dry_run=True``.
  * NOTHING in this module reads, writes, refreshes or deletes a credential
    (no keychain, no .credentials.json). Switching is Model 1: transcript carry
    only (see :mod:`claudex.core.switch`).

Flow (Phase 1, when dry_run=False):
  user types ``/switch <target>`` in the live claude
    -> UserPromptSubmit hook (`claudex _internal switch-request`) writes a
       SwitchRequest and blocks the prompt
    -> Supervisor (parent of claude) sees the request, terminates the child,
       carries the transcript into the target profile, relaunches
       ``claude --resume <sid>`` with CLAUDE_CONFIG_DIR=<target>.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDE_BIN, CLAUDE_CONFIG_DIR_ENV, CLAUDEX_HOME
from claudex.core.profile import ProfileManager, validate_profile_name
from claudex.core.switch import (
    SwitchRequest,
    carry_project_config,
    carry_transcript,
    parse_switch_command,
    resolve_transcript,
)
from claudex.exceptions import ProfileNotFoundError

SUPERVISOR_FILE = CLAUDEX_HOME / ".supervisor"
SWITCH_REQUEST_FILE = CLAUDEX_HOME / ".switch_request"

# Env the supervisor exports into its claude child so the hook knows the live
# context without re-reading on-disk state that may lag.
ENV_SUPERVISOR = "CLAUDEX_SUPERVISOR"
ENV_ACTIVE_PROFILE = "CLAUDEX_ACTIVE_PROFILE"


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@dataclass
class Liveness:
    pid: int
    profile: str
    model: int = 1

    def to_json(self) -> str:
        return json.dumps({"pid": self.pid, "profile": self.profile, "model": self.model})

    @classmethod
    def read(cls, path: Path = SUPERVISOR_FILE) -> Optional["Liveness"]:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls(pid=int(d["pid"]), profile=str(d.get("profile", "")),
                       model=int(d.get("model", 1)))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def write(self, path: Path = SUPERVISOR_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def supervisor_alive(path: Path = SUPERVISOR_FILE) -> bool:
    live = Liveness.read(path)
    return bool(live and is_pid_alive(live.pid))


# ---------------------------------------------------------------------------
# Hook (de)registration in a profile's settings.json
# ---------------------------------------------------------------------------

HOOK_COMMAND = "claudex _internal switch-request"
_HOOK_BACKUP_SUFFIX = ".claudex-switch-bak"


def _hook_entry() -> dict:
    return {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}


def _has_our_hook(settings: dict) -> bool:
    groups = (settings.get("hooks") or {}).get("UserPromptSubmit") or []
    for group in groups:
        for h in (group.get("hooks") or []):
            if h.get("type") == "command" and h.get("command") == HOOK_COMMAND:
                return True
    return False


def ensure_hook(config_dir: Path) -> bool:
    """Idempotently add the /switch hook to ``config_dir/settings.json``.

    Backs up the original to ``settings.json.claudex-switch-bak`` (once) before
    the first modification. Returns True if a change was written. Preserves any
    existing user hooks. Touches no credentials.
    """
    settings_path = config_dir / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, json.JSONDecodeError):
            settings = {}
    if not isinstance(settings, dict):
        settings = {}
    if _has_our_hook(settings):
        return False

    backup = config_dir / f"settings.json{_HOOK_BACKUP_SUFFIX}"
    if settings_path.exists() and not backup.exists():
        backup.write_bytes(settings_path.read_bytes())

    hooks = settings.setdefault("hooks", {})
    ups = hooks.setdefault("UserPromptSubmit", [])
    ups.append(_hook_entry())
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def restore_hook(config_dir: Path) -> None:
    """Undo :func:`ensure_hook`: restore the backup if present, else strip only
    our hook entry (leaving any user hooks intact)."""
    settings_path = config_dir / "settings.json"
    backup = config_dir / f"settings.json{_HOOK_BACKUP_SUFFIX}"
    if backup.exists():
        settings_path.write_bytes(backup.read_bytes())
        try:
            backup.unlink()
        except OSError:
            pass
        return
    # No backup → there was no settings.json before, or we never backed up.
    if not settings_path.exists():
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, json.JSONDecodeError):
        return
    groups = (settings.get("hooks") or {}).get("UserPromptSubmit")
    if not groups:
        return
    kept = [g for g in groups if not any(
        h.get("command") == HOOK_COMMAND for h in (g.get("hooks") or [])
    )]
    if kept:
        settings["hooks"]["UserPromptSubmit"] = kept
    else:
        settings["hooks"].pop("UserPromptSubmit", None)
        if not settings["hooks"]:
            settings.pop("hooks", None)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Target resolution shared by hook + supervisor
# ---------------------------------------------------------------------------

def resolve_target_name(pm: ProfileManager, token: str) -> Optional[str]:
    """Resolve a /switch argument to a profile name. Accepts an exact name or
    a 1-based index into the sorted profile list. Returns None if unresolved."""
    token = token.strip()
    if not token:
        return None
    if pm.exists(token):
        return token
    if token.isdigit():
        profiles = pm.list()
        idx = int(token)
        if 1 <= idx <= len(profiles):
            return profiles[idx - 1].name
    return None


# ---------------------------------------------------------------------------
# Hook handler (claudex _internal switch-request)
# ---------------------------------------------------------------------------

def _block(reason: str) -> str:
    # UserPromptSubmit: decision=block prevents the prompt from reaching the
    # model/slash-router and shows `reason` to the user.
    return json.dumps({"decision": "block", "reason": reason})


def handle_switch_hook(
    stdin_text: str,
    pm: Optional[ProfileManager] = None,
    request_path: Path = SWITCH_REQUEST_FILE,
    supervisor_file: Path = SUPERVISOR_FILE,
) -> tuple[str, int]:
    """Pure-ish hook handler. Returns ``(stdout_text, exit_code)``.

    * Non-``/switch`` prompt -> ``("", 0)`` passthrough (hook stays invisible).
    * ``/switch`` -> writes a SwitchRequest and returns a block decision, or a
      block explaining why it can't (no supervisor / unknown profile / etc.).

    Writes nothing anywhere except the request file, and only on a valid switch.
    """
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, json.JSONDecodeError):
        return ("", 0)  # malformed input: never interfere with the prompt
    if not isinstance(data, dict):
        return ("", 0)

    prompt = data.get("prompt", "")
    target_arg = parse_switch_command(prompt)
    if target_arg is None:
        return ("", 0)  # not a switch command

    # From here on it IS a /switch — we always block the prompt.
    live = Liveness.read(supervisor_file)
    if not live or not is_pid_alive(live.pid):
        return (_block("⚠️  switch needs a claudex supervisor — start your "
                       "session with `claudex run <profile>`."), 0)

    if "," in target_arg:
        return (_block("Multi-switch (switch 1,3,5) is not wired up yet — "
                       "switch to one profile at a time for now."), 0)

    pm = pm or ProfileManager()
    name = resolve_target_name(pm, target_arg)
    if name is None:
        return (_block(f"Unknown profile '{target_arg}'. Run `claudex list`."), 0)
    if name == live.profile:
        return (_block(f"Already on profile '{name}'."), 0)

    req = SwitchRequest(
        target=name,
        session_id=str(data.get("session_id", "")),
        cwd=str(data.get("cwd", "")),
        transcript_path=str(data.get("transcript_path", "")),
        source_profile=live.profile,
        model=live.model,
    )
    req.write(request_path)
    return (_block(f"🔄 Switching to '{name}'… the supervisor will reopen this "
                   f"conversation under that profile."), 0)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

@dataclass
class PlannedLaunch:
    target: str
    cmd: list[str]
    env_config_dir: str
    resumed_session: Optional[str]
    carried_from: Optional[str]  # source transcript path, if a carry happened
    carry_blocks_stripped: int = 0

    def describe(self) -> str:
        bits = [f"profile={self.target}", f"CLAUDE_CONFIG_DIR={self.env_config_dir}"]
        if self.resumed_session:
            bits.append(f"--resume {self.resumed_session}")
        else:
            bits.append("(fresh session — transcript not resolved)")
        if self.carried_from:
            bits.append(f"carried transcript (stripped {self.carry_blocks_stripped} sig blocks)")
        return "  ".join(bits)


class Supervisor:
    """Owns the claude child and applies switch requests between relaunches."""

    def __init__(
        self,
        profile_name: str,
        model: int = 1,
        dry_run: bool = False,
        extra_args: Optional[list[str]] = None,
        poll_seconds: float = 0.5,
        pm: Optional[ProfileManager] = None,
        request_path: Path = SWITCH_REQUEST_FILE,
        supervisor_file: Path = SUPERVISOR_FILE,
        claude_bin: str = CLAUDE_BIN,
        install_hook: bool = True,
        proxy_override: Optional[bool] = None,
    ) -> None:
        validate_profile_name(profile_name)
        self.pm = pm or ProfileManager()
        if not self.pm.exists(profile_name):
            raise ProfileNotFoundError(profile_name)
        self.active_profile = profile_name
        self.model = model
        self.dry_run = dry_run
        self.extra_args = list(extra_args or [])
        self.poll_seconds = poll_seconds
        self.request_path = request_path
        self.supervisor_file = supervisor_file
        self.claude_bin = claude_bin
        self.install_hook = install_hook
        self._hooked_dirs: set[Path] = set()
        self._child: Optional[subprocess.Popen] = None
        self._shutdown = False
        # None → honor each profile's stored flag; True/False → force for this run.
        self._proxy_override = proxy_override

    # -- liveness --------------------------------------------------------
    def _write_liveness(self) -> None:
        Liveness(pid=os.getpid(), profile=self.active_profile,
                 model=self.model).write(self.supervisor_file)

    def _clear_liveness(self) -> None:
        try:
            if self.supervisor_file.exists():
                cur = Liveness.read(self.supervisor_file)
                # Only clear if it's ours — don't nuke another supervisor's file.
                if cur and cur.pid == os.getpid():
                    self.supervisor_file.unlink()
        except OSError:
            pass

    # -- target preparation (NO process control, NO credentials) ---------
    def prepare_target(self, req: SwitchRequest) -> PlannedLaunch:
        target = self.pm.get(req.target)
        source = self.pm.get(req.source_profile) if req.source_profile and \
            self.pm.exists(req.source_profile) else None

        carried_from: Optional[str] = None
        stripped = 0
        if self.model == 1 and source is not None and req.session_id and req.cwd:
            # ALWAYS carry the latest transcript from the profile we're leaving,
            # overwriting any stale copy in the target. The session is one growing
            # file, so the source (where the newest turns were just written) is
            # authoritative; a target copy from an earlier visit is stale and
            # would resume a conversation missing everything typed since. Source
            # is only ever read — never moved. (Fixes round-trip loss: A→B→A.)
            try:
                result = carry_transcript(
                    source.config_dir, target.config_dir,
                    req.session_id, req.cwd, strip=True,
                )
                carried_from = str(result.source)
                stripped = result.stats.blocks_stripped
            except FileNotFoundError:
                # Source has no transcript yet (session never materialised) —
                # nothing to carry; fall through to resume target's own copy if
                # it happens to have one, else launch fresh.
                carried_from = None
            # Carry the per-project trust + tool approvals so the target profile
            # doesn't re-prompt the folder-trust dialog on every switch. Local
            # file edit only; never touches identity or credentials.
            try:
                carry_project_config(source.config_dir, target.config_dir, req.cwd)
            except OSError:
                pass

        resolved = None
        if req.session_id and resolve_transcript(target.config_dir, req.session_id, req.cwd):
            resolved = req.session_id

        cmd = [self.claude_bin]
        if resolved:
            cmd += ["--resume", resolved]
        cmd += self.extra_args

        return PlannedLaunch(
            target=req.target,
            cmd=cmd,
            env_config_dir=str(target.config_dir),
            resumed_session=resolved,
            carried_from=carried_from,
            carry_blocks_stripped=stripped,
        )

    def _consume_request(self) -> Optional[SwitchRequest]:
        req = SwitchRequest.read(self.request_path)
        if req is None:
            return None
        try:
            self.request_path.unlink()
        except OSError:
            pass
        return req

    # -- hook lifecycle --------------------------------------------------
    def _ensure_hook(self, config_dir: Path) -> None:
        if not self.install_hook:
            return
        try:
            if ensure_hook(config_dir):
                self._hooked_dirs.add(config_dir)
            else:
                # Already had it (user-installed); don't claim ownership.
                pass
        except OSError:
            pass

    def _restore_hooks(self) -> None:
        for d in list(self._hooked_dirs):
            try:
                restore_hook(d)
            except OSError:
                pass
        self._hooked_dirs.clear()

    # -- process control -------------------------------------------------
    def _proxy_base_for(self, profile: str) -> Optional[str]:
        """Return the shared proxy daemon's base URL for *profile* (auto-started),
        or None if proxy mode is not in effect for it."""
        from claudex.core.policy import proxy_enabled
        want = self._proxy_override
        if want is None:
            want = proxy_enabled(profile)
        if not want:
            return None
        from claudex.core.proxy import ensure_shared
        return ensure_shared()

    def _env_for(self, config_dir: str, profile: str) -> dict:
        env = {**os.environ,
               CLAUDE_CONFIG_DIR_ENV: config_dir,
               ENV_SUPERVISOR: "1",
               ENV_ACTIVE_PROFILE: profile}
        # Apply the profile's remote-settings policy (no-op unless set). Touches
        # no credentials — only NODE_OPTIONS, the shim, and the profile's own
        # remote-settings.json cache.
        from claudex.core.policy import augment_env
        augment_env(env, profile, config_dir=Path(config_dir))
        # Proxy mode: route through the shared proxy daemon so the org managed
        # policy is skipped (fail-open orgs) and telemetry is off; inference
        # still reaches the real API through the proxy.
        base = self._proxy_base_for(profile)
        if base:
            from claudex.core.proxy import proxy_env
            env.update(proxy_env(base))
        return env

    def _run_child_until_switch_or_exit(
        self, cmd: list[str], config_dir: str, profile: str,
    ) -> tuple[Optional[SwitchRequest], int]:
        """Launch claude and block until it exits. A background watcher polls
        for a switch request and SIGTERMs the child when one arrives.

        Returns (switch_request | None, returncode). A non-None request means
        the child was terminated to apply a switch; None means natural exit.
        """
        # Drop any stale request so we don't instantly "switch" on launch.
        try:
            self.request_path.unlink()
        except OSError:
            pass

        child = subprocess.Popen(cmd, env=self._env_for(config_dir, profile))
        self._child = child
        stop = threading.Event()
        found: dict[str, SwitchRequest] = {}

        def watch() -> None:
            while not stop.is_set():
                if child.poll() is not None:
                    return
                req = SwitchRequest.read(self.request_path)
                if req is not None:
                    found["req"] = req
                    try:
                        self.request_path.unlink()
                    except OSError:
                        pass
                    try:
                        child.send_signal(signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    return
                time.sleep(self.poll_seconds)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            rc = child.wait()
        finally:
            stop.set()
            watcher.join(timeout=5)
            self._child = None
        return found.get("req"), rc

    def _install_signal_handlers(self) -> None:
        """Convert SIGTERM/SIGHUP into a clean shutdown so the finally block
        (restore hooks, clear liveness) always runs. Without this, closing the
        terminal (SIGHUP) terminates Python with no cleanup, leaving the hook
        installed in every visited profile. No-op off the main thread (tests).
        """
        def _on_signal(_signum, _frame):
            self._shutdown = True
            if self._child is not None:
                try:
                    self._child.send_signal(signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass  # not main thread / unsupported

    # -- loops -----------------------------------------------------------
    def run(self) -> int:
        """Live supervisor loop. Hosts claude; applies switches between runs.

        Launches claude under the active profile, and whenever the hook files a
        switch request, terminates it, carries the transcript into the target
        (Model 1), and relaunches ``claude --resume``. Returns claude's exit
        code when it exits without a pending switch. Touches no credentials.
        """
        self._install_signal_handlers()
        self._ensure_hook(self.pm.get(self.active_profile).config_dir)
        self._write_liveness()
        cmd = [self.claude_bin, *self.extra_args]
        config_dir = str(self.pm.get(self.active_profile).config_dir)
        profile = self.active_profile
        rc = 0
        try:
            while not self._shutdown:
                req, rc = self._run_child_until_switch_or_exit(cmd, config_dir, profile)
                if self._shutdown or req is None:
                    return rc  # shutdown signal, or claude exited on its own
                plan = self.prepare_target(req)
                self.active_profile = req.target
                profile = req.target
                config_dir = plan.env_config_dir
                cmd = plan.cmd
                self._ensure_hook(self.pm.get(profile).config_dir)
                self._write_liveness()
            return rc
        finally:
            self._clear_liveness()
            self._restore_hooks()

    def run_dry(self, timeout: Optional[float] = None) -> Optional[PlannedLaunch]:
        """Dry-run: register liveness, wait for one switch request, print the
        launch plan, and return it WITHOUT spawning or killing anything.

        Returns the PlannedLaunch (also when a request is already pending), or
        None if *timeout* elapsed with no request.
        """
        self._write_liveness()
        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                req = self._consume_request()
                if req is not None:
                    plan = self.prepare_target(req)
                    self.active_profile = req.target
                    self._write_liveness()
                    return plan
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                time.sleep(self.poll_seconds)
        finally:
            self._clear_liveness()
