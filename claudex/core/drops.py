"""Auth drops — log in on one machine, land the credentials in a profile on another.

Machine B (where the profile lives) runs ``claudex auth remote <profile>``: it
creates a one-shot mailbox ("drop") on the sharing server, keeps an X25519 key
pair locally and prints ONE line for machine A. Machine A runs
``claudex auth fulfill <token>`` (or ``aimux auth fulfill``, same token): the
usual interactive ``claude /login`` happens there, the resulting
``.credentials.json`` + ``.claude.json`` are sealed to B's public key and
uploaded with the drop's single-use write token — A needs no account on the
server. B polls, unseals, installs the files and deletes the drop.

Security: the one-liner carries the server URL, the drop id, the write token
and B's PUBLIC key. The server burns the write token on the first successful
write; the private key never leaves B. A leaked line can neither be replayed
nor decrypt anything.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import claudex.constants as C
from claudex.core.sharing_client import SharingAPIError, SharingClient, fill_drop, load_client
from claudex.crypto import decode_drop_token, encode_drop_token, generate_keypair, seal, unseal
from claudex.exceptions import AuthError, ClaudexError

DEFAULT_TTL_MINUTES = 15
POLL_SECONDS = 3.0
#: what a Claude login leaves behind that another machine needs
AUTH_FILES = (".credentials.json", ".claude.json")


@dataclass
class DropRequest:
    """What machine B keeps while waiting — the private key stays here."""

    profile_name: str
    backend: str
    endpoint: str
    drop_id: str
    private_key: str
    token: str
    expires_at: str


# ── machine B ────────────────────────────────────────────────────────────────

def request_drop(profile_name: str, endpoint: str, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> DropRequest:
    """Create the profile if needed and a drop for it; returns what to print."""
    from claudex.core.profile import ProfileManager

    pm = ProfileManager()
    if not pm.exists(profile_name):
        pm.create(profile_name)
    client = load_client(endpoint)
    private_key, public_key = generate_keypair()
    created = client.create_drop(label=profile_name, backend="claude", ttl_minutes=ttl_minutes)
    token = encode_drop_token(endpoint, created["drop_id"], created["write_token"], public_key, "claude")
    return DropRequest(
        profile_name=profile_name, backend="claude", endpoint=endpoint.rstrip("/"),
        drop_id=created["drop_id"], private_key=private_key, token=token,
        expires_at=str(created.get("expires_at") or ""),
    )


def wait_for_drop(req: DropRequest, timeout_s: float, poll_s: float = POLL_SECONDS,
                  on_tick: Optional[Callable[[], None]] = None):
    """Poll until machine A has written, then install the credentials.

    Returns the updated Profile. Raises ClaudexError on expiry/timeout.
    """
    from claudex.core.auth import AuthManager
    from claudex.core.profile import ProfileManager

    client: SharingClient = load_client(req.endpoint)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            filled = client.get_drop(req.drop_id)
        except SharingAPIError as e:
            if e.status_code == 410:
                raise ClaudexError("The drop expired before the other machine wrote to it")
            raise
        if filled and filled.get("encrypted_data"):
            break
        if time.monotonic() >= deadline:
            raise ClaudexError("Timed out waiting for the other machine")
        if on_tick:
            on_tick()
        time.sleep(poll_s)
    backend = str(filled.get("backend") or "claude")
    if backend != "claude":
        raise ClaudexError(f"Drop carries {backend} credentials; claudex profiles are Claude-only")
    bundle = unseal(req.private_key, base64.b64decode(filled["encrypted_data"]))
    pm = ProfileManager()
    profile = pm.get(req.profile_name)
    install_auth_bundle(profile.config_dir, bundle)
    am = AuthManager()
    am._import_claude_credentials(profile.name, profile.config_dir)
    auth_type, email, _, _, _ = am._read_claude_creds(profile.config_dir)
    profile.auth_type = auth_type if auth_type != "none" else profile.auth_type
    email = email or _email_from_claude_json(profile.config_dir)
    if email:
        profile.email = email
    profile.save()
    try:
        client.delete_drop(req.drop_id)
    except SharingAPIError:
        pass
    return profile


def install_auth_bundle(config_dir: Path, bundle: bytes) -> list:
    """Write ``.credentials.json`` and merge ``.claude.json`` into *config_dir*."""
    config_dir = config_dir.resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            if not member.isreg() or member.name != name or name not in AUTH_FILES:
                continue
            src = tar.extractfile(member)
            if src is None:
                continue
            data = src.read()
            target = config_dir / name
            if name == ".claude.json" and target.exists():
                data = _merge_claude_json(target.read_bytes(), data)
            target.write_bytes(data)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            written.append(name)
    return written


def _email_from_claude_json(config_dir: Path) -> str:
    try:
        data = json.loads((config_dir / ".claude.json").read_text(encoding="utf-8"))
        return str((data.get("oauthAccount") or {}).get("emailAddress") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _merge_claude_json(existing: bytes, incoming: bytes) -> bytes:
    """Account identity + onboarding flags from the login machine; this
    profile's own projects/trust settings are kept."""
    try:
        base = json.loads(existing)
        new = json.loads(incoming)
    except ValueError:
        return incoming
    if not isinstance(base, dict) or not isinstance(new, dict):
        return incoming
    for key, value in new.items():
        if key != "projects":
            base[key] = value
    return json.dumps(base, indent=2).encode("utf-8")


# ── machine A ────────────────────────────────────────────────────────────────

def fulfill_drop(token: str, login_runner: Optional[Callable[[dict], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None) -> str:
    """Run ``claude /login`` here in a temporary config dir and upload the result.

    *login_runner(env)* is injectable for tests. Returns the backend name.
    The temporary dir and the Keychain entry Claude creates for it are removed.
    """
    from claudex.core.auth import AuthManager

    info = decode_drop_token(token)
    if info["b"] != "claude":
        raise ClaudexError(f"This drop expects {info['b']} credentials; claudex can only log in to Claude "
                           f"(use `aimux auth fulfill` for other backends)")
    drop_id = str(info["d"])
    work_dir = C.CLAUDEX_HOME / ".drops" / drop_id
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    temp_name = f"drop-{drop_id[:8]}"
    am = AuthManager()
    try:
        env = {**os.environ, C.CLAUDE_CONFIG_DIR_ENV: str(work_dir)}
        from claudex.core.config import global_launch_env

        env.update(global_launch_env())
        if on_status:
            on_status("Starting Claude login in a temporary profile…")
        if login_runner is None:
            if not am._claude_available():
                from claudex.exceptions import ClaudeNotFoundError

                raise ClaudeNotFoundError()
            subprocess.run([C.CLAUDE_BIN, "/login"], env=env, check=False)
        else:
            login_runner(env)
        # Keychain (macOS) -> .credentials.json so the file set is complete.
        # Deliberately NOT AuthManager.flush_credentials_to_file(): it falls
        # back to ~/.claude (the home account) when the dir is empty, which
        # must never be uploaded when the user aborted the login. Only the
        # Keychain entry derived from THIS temp dir is consulted.
        creds_path = work_dir / ".credentials.json"
        if C.IS_MACOS and not creds_path.exists():
            blob = am._read_macos_keychain_blob(work_dir)
            if isinstance(blob, dict) and (blob.get("claudeAiOauth") or {}).get("accessToken"):
                creds_path.write_text(json.dumps(blob), encoding="utf-8")
                creds_path.chmod(0o600)
        if not _has_access_token(creds_path):
            raise AuthError("Claude login did not produce credentials; nothing uploaded")
        bundle = _pack(work_dir)
        sealed = seal(str(info["k"]), bundle)
        if on_status:
            on_status(f"Uploading sealed credentials to {info['e']}…")
        fill_drop(str(info["e"]), drop_id, str(info["w"]), sealed, "claude")
    finally:
        _cleanup(am, temp_name, work_dir)
    return "claude"


def _has_access_token(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    block = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return bool(isinstance(block, dict) and block.get("accessToken"))


def _pack(config_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in AUTH_FILES:
            path = config_dir / name
            if path.is_file():
                tar.add(path, arcname=name, recursive=False)
    return buf.getvalue()


def _cleanup(am, temp_name: str, work_dir: Path) -> None:
    for key in ("oauth_token", "api_key", "refresh_token", "expires_at", "auth_type", "email"):
        try:
            am.backend.delete(temp_name, key)
        except Exception:
            pass
    if C.IS_MACOS:
        try:
            service, account = am._macos_keychain_service(work_dir)
            subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                           capture_output=True, timeout=5)
        except Exception:
            pass
    shutil.rmtree(work_dir, ignore_errors=True)
