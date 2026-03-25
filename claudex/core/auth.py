"""Auth manager — token lifecycle, OAuth and API key management."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from claudex.constants import CLAUDE_CONFIG_DIR_ENV, CLAUDE_BIN, IS_MACOS, IS_WINDOWS
from claudex.exceptions import AuthError, ClaudeNotFoundError
from claudex.platform import get_credential_backend
from claudex.platform.base import CredentialBackend


@dataclass
class AuthStatus:
    profile_name: str
    auth_type: Literal["oauth", "api_key", "none"]
    email: str
    expires_at: Optional[datetime]
    is_expired: bool
    refresh_available: bool
    raw_token_preview: str  # first/last 8 chars only

    @property
    def expires_in_human(self) -> str:
        if self.expires_at is None:
            if self.auth_type == "api_key":
                return "never"
            # OAuth via OS keychain (macOS/Windows) — expiry managed by the OS
            if self.auth_type == "oauth" and not self.raw_token_preview:
                return "managed by OS"
            return "unknown"
        now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - now
        if delta.total_seconds() < 0:
            return "EXPIRED"
        total = int(delta.total_seconds())
        days = total // 86400
        hours = (total % 86400) // 3600
        minutes = (total % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


class AuthManager:
    def __init__(self) -> None:
        self._backend: Optional[CredentialBackend] = None

    @property
    def backend(self) -> CredentialBackend:
        if self._backend is None:
            self._backend = get_credential_backend()
        return self._backend

    def add_account_oauth(self, profile_name: str, config_dir: Path) -> None:
        """Spawn `claude /login` inside the profile's CLAUDE_CONFIG_DIR."""
        if not self._claude_available():
            raise ClaudeNotFoundError()
        env = {**os.environ, CLAUDE_CONFIG_DIR_ENV: str(config_dir)}
        try:
            subprocess.run([CLAUDE_BIN, "/login"], env=env, check=False)
        except FileNotFoundError:
            raise ClaudeNotFoundError()
        # After login, read the OAuth token Claude stored
        self._import_claude_credentials(profile_name, config_dir)

    def add_api_key(self, profile_name: str, api_key: str) -> None:
        """Store a raw API key for a profile."""
        if not api_key.startswith("sk-ant-"):
            raise AuthError("Invalid Anthropic API key format (should start with sk-ant-)")
        self.backend.store(profile_name, "api_key", api_key)
        self.backend.store(profile_name, "auth_type", "api_key")

    def get_status(self, profile_name: str, config_dir: Path) -> AuthStatus:
        auth_type_stored = self.backend.retrieve(profile_name, "auth_type") or "none"
        email = self.backend.retrieve(profile_name, "email") or ""
        expires_str = self.backend.retrieve(profile_name, "expires_at") or ""
        token = self.backend.retrieve(profile_name, "oauth_token") or \
                self.backend.retrieve(profile_name, "api_key") or ""

        expires_at: Optional[datetime] = None
        if expires_str:
            try:
                expires_at = datetime.fromisoformat(expires_str)
            except ValueError:
                pass

        is_expired = False
        if expires_at:
            now = datetime.now(timezone.utc)
            exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            is_expired = exp < now

        # Try reading directly from Claude's credential files if no stored creds
        if not token and auth_type_stored == "none":
            auth_type_stored, email, expires_at, token = self._read_claude_creds(config_dir)

        preview = ""
        if token and len(token) > 16:
            preview = f"{token[:8]}...{token[-8:]}"

        refresh = bool(self.backend.retrieve(profile_name, "refresh_token"))

        return AuthStatus(
            profile_name=profile_name,
            auth_type=auth_type_stored,  # type: ignore[arg-type]
            email=email,
            expires_at=expires_at,
            is_expired=is_expired,
            refresh_available=refresh,
            raw_token_preview=preview,
        )

    def get_env_for_profile(self, profile_name: str, config_dir: Path) -> dict[str, str]:
        """Return env vars to inject when launching claude for this profile."""
        env: dict[str, str] = {CLAUDE_CONFIG_DIR_ENV: str(config_dir)}
        api_key = self.backend.retrieve(profile_name, "api_key")
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        # NOTE: Subscription OAuth tokens are intentionally NOT injected via
        # CLAUDE_CODE_OAUTH_TOKEN.  That env var signals API-level OAuth to Claude
        # Code (displays as "Claude API"), whereas subscription ("Claude Pro") auth
        # works by Claude reading .credentials.json from CLAUDE_CONFIG_DIR directly.
        return env

    def revoke(self, profile_name: str) -> None:
        for key in ["oauth_token", "refresh_token", "api_key", "auth_type", "email", "expires_at"]:
            self.backend.delete(profile_name, key)

    def _import_claude_credentials(self, profile_name: str, config_dir: Path) -> None:
        """Read credentials Claude wrote during /login and store them in our backend."""
        auth_type, email, expires_at, token = self._read_claude_creds(config_dir)
        if token:
            self.backend.store(profile_name, "oauth_token", token)
        if auth_type != "none":
            self.backend.store(profile_name, "auth_type", auth_type)
        if email:
            self.backend.store(profile_name, "email", email)
        if expires_at:
            self.backend.store(profile_name, "expires_at", expires_at.isoformat())
        # Move temp refresh token if it was stashed during _read_claude_creds
        refresh = self.backend.retrieve("_tmp_refresh", "refresh_token")
        if refresh:
            self.backend.store(profile_name, "refresh_token", refresh)
            self.backend.delete("_tmp_refresh", "refresh_token")

    def _read_claude_creds(
        self, config_dir: Path
    ) -> tuple[str, str, Optional[datetime], str]:
        """Try to read Claude's stored credentials from its config dir."""
        candidates = [
            config_dir / ".credentials.json",
            config_dir / "credentials.json",
            Path.home() / ".claude" / ".credentials.json",  # default Claude dir
            Path.home() / ".claude.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))

                # Real Claude format: {"claudeAiOauth": {"accessToken": ..., "expiresAt": <ms>}}
                oauth_block = data.get("claudeAiOauth") or {}
                if isinstance(oauth_block, dict) and oauth_block.get("accessToken"):
                    token = oauth_block["accessToken"]
                    refresh = oauth_block.get("refreshToken", "")
                    expires_ms = oauth_block.get("expiresAt")
                    expires_at: Optional[datetime] = None
                    if isinstance(expires_ms, (int, float)):
                        expires_at = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
                    email = data.get("emailAddress", "")
                    auth_type = "oauth" if token.startswith("sk-ant-oat") else "api_key"
                    # Also store refresh token
                    if refresh:
                        self.backend.store("_tmp_refresh", "refresh_token", refresh)
                    return auth_type, email, expires_at, token

                # Flat format fallback: {"accessToken": ..., "oauthToken": ...}
                token = data.get("accessToken") or data.get("oauthToken", "")
                email = data.get("emailAddress", "")
                expires_raw = data.get("expiresAt", "")
                expires_at = None
                if isinstance(expires_raw, (int, float)):
                    expires_at = datetime.fromtimestamp(expires_raw / 1000, tz=timezone.utc)
                elif isinstance(expires_raw, str) and expires_raw:
                    try:
                        expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
                    except ValueError:
                        pass
                auth_type = "oauth" if token and token.startswith("sk-ant-oat") else \
                            "api_key" if token and token.startswith("sk-ant-") else "none"
                if token:
                    return auth_type, email, expires_at, token
            except Exception:
                continue

        # On macOS/Windows, Claude Code stores the real token in the OS credential
        # store under "Claude Code-credentials-{sha256(config_dir)[:8]}".
        # Try that before falling back to metadata-only detection.
        if IS_MACOS:
            keychain_data = self._read_macos_keychain(config_dir)
            if keychain_data:
                return keychain_data
        elif IS_WINDOWS:
            wincred_data = self._read_windows_credential_manager(config_dir)
            if wincred_data:
                return wincred_data

        # Claude Code also writes state to .claude.json inside the config dir.
        # If keychain read failed (e.g. user denied access), fall back to
        # metadata-only: oauthAccount gives us email + billing type so we can
        # at least show "oauth" instead of "None" in the auth manager.
        dot_claude = config_dir / ".claude.json"
        if dot_claude.exists():
            try:
                data = json.loads(dot_claude.read_text(encoding="utf-8"))
                oauth_account = data.get("oauthAccount") or {}
                if isinstance(oauth_account, dict) and oauth_account.get("accountUuid"):
                    email = oauth_account.get("emailAddress", "")
                    billing = oauth_account.get("billingType", "")
                    auth_type = "api_key" if billing == "api_key" else "oauth"
                    return auth_type, email, None, ""
            except Exception:
                pass

        return "none", "", None, ""

    def _read_macos_keychain(
        self, config_dir: Path
    ) -> Optional[tuple[str, str, Optional[datetime], str]]:
        """Read Claude Code's OAuth token from the macOS Keychain.

        Claude Code stores credentials under the service name:
          "Claude Code-credentials-{sha256(config_dir_path)[:8]}"
        with the current OS username as the account.

        Falls back to the un-suffixed "Claude Code-credentials" service
        (used by the default ~/.claude profile).
        """
        import getpass
        suffix = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
        account = getpass.getuser()
        services = [
            f"Claude Code-credentials-{suffix}",
            "Claude Code-credentials",
        ]
        for service in services:
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    continue
                raw = result.stdout.strip()
                data = json.loads(raw)

                oauth_block = data.get("claudeAiOauth") or {}
                if not isinstance(oauth_block, dict) or not oauth_block.get("accessToken"):
                    continue

                token: str = oauth_block["accessToken"]
                refresh: str = oauth_block.get("refreshToken", "")
                expires_ms = oauth_block.get("expiresAt")
                expires_at: Optional[datetime] = None
                if isinstance(expires_ms, (int, float)):
                    expires_at = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
                # Email lives in .claude.json → oauthAccount, not in the keychain blob
                email: str = data.get("emailAddress", "")
                if not email:
                    dot_claude = config_dir / ".claude.json"
                    try:
                        dc = json.loads(dot_claude.read_text(encoding="utf-8"))
                        email = (dc.get("oauthAccount") or {}).get("emailAddress", "")
                    except Exception:
                        pass
                auth_type = "oauth" if token.startswith("sk-ant-oat") else "api_key"
                if refresh:
                    self.backend.store("_tmp_refresh", "refresh_token", refresh)
                return auth_type, email, expires_at, token
            except Exception:
                continue
        return None

    def _read_windows_credential_manager(
        self, config_dir: Path
    ) -> Optional[tuple[str, str, Optional[datetime], str]]:
        """Read Claude Code's OAuth token from the Windows Credential Manager.

        Claude Code (via keytar) stores credentials with a TargetName of:
          "Claude Code-credentials-{sha256(config_dir)[:8]}/{username}"
        falling back to the un-suffixed service name for the default profile.

        The CredentialBlob is the JSON payload encoded as UTF-16 LE.
        """
        import ctypes
        import ctypes.wintypes
        import getpass

        CRED_TYPE_GENERIC = 1

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.wintypes.DWORD),
                        ("dwHighDateTime", ctypes.wintypes.DWORD)]

        class _CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags",              ctypes.wintypes.DWORD),
                ("Type",               ctypes.wintypes.DWORD),
                ("TargetName",         ctypes.wintypes.LPWSTR),
                ("Comment",            ctypes.wintypes.LPWSTR),
                ("LastWritten",        _FILETIME),
                ("CredentialBlobSize", ctypes.wintypes.DWORD),
                ("CredentialBlob",     ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist",            ctypes.wintypes.DWORD),
                ("AttributeCount",     ctypes.wintypes.DWORD),
                ("Attributes",         ctypes.c_void_p),
                ("TargetAlias",        ctypes.wintypes.LPWSTR),
                ("UserName",           ctypes.wintypes.LPWSTR),
            ]

        advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
        suffix = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
        account = getpass.getuser()
        # keytar uses "{service}/{account}" as the TargetName; also try bare service
        targets = [
            f"Claude Code-credentials-{suffix}/{account}",
            f"Claude Code-credentials/{account}",
            f"Claude Code-credentials-{suffix}",
            "Claude Code-credentials",
        ]
        for target in targets:
            cred_ptr = ctypes.c_void_p(None)
            ok = advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
            if not ok or not cred_ptr.value:
                continue
            try:
                cred = ctypes.cast(cred_ptr, ctypes.POINTER(_CREDENTIAL)).contents
                blob_size: int = cred.CredentialBlobSize
                if blob_size == 0:
                    continue
                blob = bytes(
                    ctypes.cast(cred.CredentialBlob,
                                ctypes.POINTER(ctypes.c_ubyte * blob_size)).contents
                )
                # keytar encodes the value as UTF-16 LE
                try:
                    raw = blob.decode("utf-16-le")
                except UnicodeDecodeError:
                    raw = blob.decode("utf-8", errors="replace")
                data = json.loads(raw)
                oauth_block = data.get("claudeAiOauth") or {}
                if not isinstance(oauth_block, dict) or not oauth_block.get("accessToken"):
                    continue
                token: str = oauth_block["accessToken"]
                refresh: str = oauth_block.get("refreshToken", "")
                expires_ms = oauth_block.get("expiresAt")
                expires_at: Optional[datetime] = None
                if isinstance(expires_ms, (int, float)):
                    expires_at = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
                email: str = data.get("emailAddress", "")
                if not email:
                    dot_claude = config_dir / ".claude.json"
                    try:
                        dc = json.loads(dot_claude.read_text(encoding="utf-8"))
                        email = (dc.get("oauthAccount") or {}).get("emailAddress", "")
                    except Exception:
                        pass
                auth_type = "oauth" if token.startswith("sk-ant-oat") else "api_key"
                if refresh:
                    self.backend.store("_tmp_refresh", "refresh_token", refresh)
                return auth_type, email, expires_at, token
            except Exception:
                continue
            finally:
                advapi32.CredFree(cred_ptr)
        return None

    def _claude_available(self) -> bool:
        import shutil
        return shutil.which(CLAUDE_BIN) is not None
