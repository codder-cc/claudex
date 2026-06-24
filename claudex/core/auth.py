"""Auth manager — token lifecycle, OAuth and API key management."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
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

    def refresh(self, profile_name: str, config_dir: Path) -> AuthStatus:
        """Use the stored refresh token to obtain a new access token.

        Calls the Claude OAuth token endpoint and persists the new tokens to
        both our credential backend and the profile's .credentials.json so
        Claude Code picks them up on next launch.

        Raises AuthError if no refresh token is stored or the refresh fails.
        """
        import urllib.request
        import urllib.error

        refresh_token = self.backend.retrieve(profile_name, "refresh_token")
        if not refresh_token:
            # Fall back: try reading directly from Claude's credential files
            self._import_claude_credentials(profile_name, config_dir)
            refresh_token = self.backend.retrieve(profile_name, "refresh_token")
        if not refresh_token:
            raise AuthError(
                f"No refresh token stored for profile '{profile_name}'. "
                "Run 'claudex auth add' or 'claudex auth import-current' first."
            )

        payload = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload",
        }).encode()

        # Cloudflare in front of platform.claude.com returns error 1010 ("browser
        # signature banned") for the default Python-urllib UA, so masquerade as a
        # generic CLI client. Don't drop the anthropic-beta header — the endpoint
        # is gated on it.
        from claudex import __version__ as _claudex_version  # local import to avoid cycles
        req = urllib.request.Request(
            "https://platform.claude.com/v1/oauth/token",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"claudex/{_claudex_version} (+https://github.com/codder-cc/claudex)",
                "anthropic-beta": "oauth-2025-04-20",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise AuthError(f"Token refresh failed ({e.code}): {body}") from e
        except Exception as e:
            raise AuthError(f"Token refresh request failed: {e}") from e

        access_token: str = data.get("access_token", "")
        new_refresh: str = data.get("refresh_token", refresh_token)  # keep old if not rotated
        expires_in: int = data.get("expires_in", 0)
        if not access_token:
            raise AuthError("Token refresh response missing access_token")

        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        expires_at_ms = now_ms + (expires_in * 1000) if expires_in else None
        expires_at: Optional[datetime] = None
        if expires_at_ms:
            expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)

        # Extract email from response if present
        account = data.get("account") or {}
        email = account.get("email_address", "") or self.backend.retrieve(profile_name, "email") or ""

        # Persist to our backend
        self.backend.store(profile_name, "oauth_token", access_token)
        self.backend.store(profile_name, "refresh_token", new_refresh)
        self.backend.store(profile_name, "auth_type", "oauth")
        if email:
            self.backend.store(profile_name, "email", email)
        if expires_at:
            self.backend.store(profile_name, "expires_at", expires_at.isoformat())

        # Write back to .credentials.json so Claude Code picks up the new token
        self._write_credentials_file(config_dir, access_token, new_refresh, expires_at_ms)

        # On macOS, also update the Keychain entry so Claude reads the fresh token
        if IS_MACOS:
            self._write_macos_keychain(config_dir, access_token, new_refresh, expires_at_ms)

        return self.get_status(profile_name, config_dir)

    def revoke(self, profile_name: str) -> None:
        for key in ["oauth_token", "refresh_token", "api_key", "auth_type", "email", "expires_at"]:
            self.backend.delete(profile_name, key)

    def flush_credentials_to_file(self, profile_name: str, config_dir: Path) -> bool:
        """Read the latest tokens from the credential backend (or OS Keychain) and
        write them to .credentials.json inside *config_dir*.

        Called before bundling a profile for sharing so the archive always
        contains a fresh accessToken + refreshToken regardless of where the
        tokens are normally stored (Keychain on macOS, etc.).  Includes all
        metadata fields (scopes, subscriptionType, rateLimitTier) so that the
        restored profile on the target machine is indistinguishable from a
        native login.

        Returns True if any credentials were found and written.
        """
        # On macOS, Claude Code's primary credential store is the Keychain.
        # Read the FULL Keychain blob first so we capture scopes, subscriptionType,
        # rateLimitTier and other metadata fields that Claude Code needs.
        full_oauth_block: dict = {}
        if IS_MACOS:
            keychain_data = self._read_macos_keychain_blob(config_dir) or {}
            full_oauth_block = keychain_data.get("claudeAiOauth") or {}

        # Try claudex backend for the core token fields
        access_token = (
            self.backend.retrieve(profile_name, "oauth_token") or
            self.backend.retrieve(profile_name, "api_key") or
            full_oauth_block.get("accessToken", "")
        )
        refresh_token = (
            self.backend.retrieve(profile_name, "refresh_token") or
            full_oauth_block.get("refreshToken", "")
        )
        expires_at_str = self.backend.retrieve(profile_name, "expires_at") or ""

        if not access_token:
            # Fall back to reading from .credentials.json / OS Keychain
            _, _, expires_dt, access_token = self._read_claude_creds(config_dir)
            # _read_claude_creds stashes refresh into a temp key
            tmp_refresh = self.backend.retrieve("_tmp_refresh", "refresh_token") or ""
            if tmp_refresh:
                refresh_token = tmp_refresh
                self.backend.delete("_tmp_refresh", "refresh_token")
            if expires_dt and not expires_at_str:
                expires_at_str = expires_dt.isoformat()

        if not access_token:
            return False

        expires_at_ms: Optional[float] = None
        if expires_at_str:
            try:
                dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                expires_at_ms = dt.timestamp() * 1000
            except ValueError:
                pass

        # Merge: start from the full Keychain block (preserves scopes etc.),
        # then overwrite the 3 mutable fields with freshest values.
        if full_oauth_block:
            full_oauth_block["accessToken"] = access_token
            full_oauth_block["refreshToken"] = refresh_token
            if expires_at_ms is not None:
                full_oauth_block["expiresAt"] = int(expires_at_ms)
            self._write_credentials_file_full(config_dir, full_oauth_block)
        else:
            self._write_credentials_file(config_dir, access_token, refresh_token, expires_at_ms)
        return True

    def import_credentials_from_file(self, profile_name: str, config_dir: Path) -> bool:
        """Import credentials from .credentials.json into the local credential backend.

        Called after extracting a share bundle on a new machine to populate
        the claudex keyring entry and (on macOS) the Keychain so that both
        `claudex auth status` and Claude Code itself work immediately.

        Restores the FULL claudeAiOauth block (including scopes, subscriptionType,
        rateLimitTier) so Claude Code recognises the session as fully authenticated.

        Returns True if credentials were found and imported.
        """
        # Read the full credentials file — we need ALL fields in the claudeAiOauth
        # block, not just the 3 that _read_claude_creds returns.
        cred_file = config_dir / ".credentials.json"
        full_oauth_block: dict = {}
        if cred_file.exists():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                full_oauth_block = data.get("claudeAiOauth") or {}
            except Exception:
                pass

        access_token = full_oauth_block.get("accessToken", "")
        refresh_token = full_oauth_block.get("refreshToken", "")
        expires_ms = full_oauth_block.get("expiresAt")

        if not access_token:
            # Fallback: use the standard reader
            auth_type_fb, email_fb, expires_at_fb, access_token = self._read_claude_creds(config_dir)
            refresh_token = self.backend.retrieve("_tmp_refresh", "refresh_token") or refresh_token
            if refresh_token:
                self.backend.delete("_tmp_refresh", "refresh_token")
            expires_ms = expires_at_fb.timestamp() * 1000 if expires_at_fb else None
        else:
            auth_type_fb = "oauth" if access_token.startswith("sk-ant-oat") else "api_key"
            email_fb = ""
            expires_at_fb = None
            if isinstance(expires_ms, (int, float)):
                from datetime import datetime, timezone
                expires_at_fb = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
            # Drain any stale temp key
            self.backend.delete("_tmp_refresh", "refresh_token")

        if not access_token:
            return False

        self.backend.store(profile_name, "oauth_token", access_token)
        self.backend.store(profile_name, "auth_type", auth_type_fb)
        if email_fb:
            self.backend.store(profile_name, "email", email_fb)
        if expires_at_fb:
            self.backend.store(profile_name, "expires_at", expires_at_fb.isoformat())
        if refresh_token:
            self.backend.store(profile_name, "refresh_token", refresh_token)

        # On macOS write the FULL oauth block to the Keychain (including scopes,
        # subscriptionType, rateLimitTier) so Claude Code sees a complete session.
        if IS_MACOS and full_oauth_block:
            self._write_macos_keychain_full(config_dir, full_oauth_block)
        elif IS_MACOS:
            self._write_macos_keychain(config_dir, access_token, refresh_token,
                                       expires_ms if isinstance(expires_ms, (int, float)) else None)

        return True

    def _write_credentials_file(
        self,
        config_dir: Path,
        access_token: str,
        refresh_token: str,
        expires_at_ms: Optional[float],
    ) -> None:
        """Write/merge refreshed tokens into .credentials.json in the profile dir."""
        cred_file = config_dir / ".credentials.json"
        existing: dict = {}
        if cred_file.exists():
            try:
                existing = json.loads(cred_file.read_text(encoding="utf-8"))
            except Exception:
                existing = {}

        oauth_block = existing.get("claudeAiOauth") or {}
        oauth_block["accessToken"] = access_token
        oauth_block["refreshToken"] = refresh_token
        if expires_at_ms is not None:
            oauth_block["expiresAt"] = int(expires_at_ms)
        existing["claudeAiOauth"] = oauth_block

        cred_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        # Restrict to owner-only (same as Claude Code does)
        try:
            cred_file.chmod(0o600)
        except Exception:
            pass

    def _write_credentials_file_full(self, config_dir: Path, oauth_block: dict) -> None:
        """Write a complete claudeAiOauth block (all fields) to .credentials.json."""
        cred_file = config_dir / ".credentials.json"
        existing: dict = {}
        if cred_file.exists():
            try:
                existing = json.loads(cred_file.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing["claudeAiOauth"] = oauth_block
        cred_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        try:
            cred_file.chmod(0o600)
        except Exception:
            pass

    def _macos_keychain_service(self, config_dir: Path) -> tuple[str, str]:
        """The (service, account) Claude Code uses for this profile's Keychain entry.

        NOTE: this mirrors Claude Code's reverse-engineered naming
        (Claude Code-credentials-{sha256(config_dir)[:8]}, account = login user).
        If a Claude version changes this, keychain writes will silently miss —
        which is why writes are verified and `macos_keychain_token_present` exists.
        """
        import getpass
        suffix = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
        return f"Claude Code-credentials-{suffix}", getpass.getuser()

    def _read_macos_keychain_blob(self, config_dir: Path) -> Optional[dict]:
        """Read and parse the Claude Code keychain blob for this profile, or None."""
        if not IS_MACOS:
            return None
        service, account = self._macos_keychain_service(config_dir)
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except Exception:
            return None
        return None

    def macos_keychain_token_present(self, config_dir: Path) -> Optional[bool]:
        """Whether the macOS Keychain entry Claude Code reads holds an access token.

        This is the store Claude actually consumes on macOS — distinct from
        .credentials.json / the claudex backend that `get_status` reads. Returns
        None on non-macOS. Use it to detect the "claudex says authed but Claude
        prompts to log in" cross-machine failure.
        """
        if not IS_MACOS:
            return None
        blob = self._read_macos_keychain_blob(config_dir) or {}
        return bool((blob.get("claudeAiOauth") or {}).get("accessToken"))

    def _keychain_add_verified(self, service: str, account: str, blob: str) -> bool:
        """Write a keychain item and verify it reads back. Returns success."""
        try:
            r = subprocess.run(
                ["security", "add-generic-password",
                 "-s", service, "-a", account, "-w", blob, "-U"],
                capture_output=True, timeout=5,
            )
            if r.returncode != 0:
                return False
        except Exception:
            return False
        # Verify the write actually landed (locked keychain / ACL can no-op).
        try:
            v = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            return v.returncode == 0 and bool(v.stdout.strip())
        except Exception:
            return False

    def _write_macos_keychain(
        self,
        config_dir: Path,
        access_token: str,
        refresh_token: str,
        expires_at_ms: Optional[float],
    ) -> bool:
        """Update the macOS Keychain entry Claude Code uses for this profile.

        Returns True if the entry was written and verified. Non-fatal on failure
        (.credentials.json is the fallback), but the result lets callers warn.
        """
        service, account = self._macos_keychain_service(config_dir)
        existing = self._read_macos_keychain_blob(config_dir) or {}
        oauth_block = existing.get("claudeAiOauth") or {}
        oauth_block["accessToken"] = access_token
        oauth_block["refreshToken"] = refresh_token
        if expires_at_ms is not None:
            oauth_block["expiresAt"] = int(expires_at_ms)
        existing["claudeAiOauth"] = oauth_block
        return self._keychain_add_verified(service, account, json.dumps(existing))

    def _write_macos_keychain_full(self, config_dir: Path, oauth_block: dict) -> bool:
        """Write a complete claudeAiOauth block (all fields) to the macOS Keychain.

        Used during profile import to restore scopes, subscriptionType, rateLimitTier
        and any other metadata that Claude Code needs to show a fully authenticated
        session rather than "Not logged in". Returns True if written and verified.
        """
        service, account = self._macos_keychain_service(config_dir)
        blob = json.dumps({"claudeAiOauth": oauth_block})
        return self._keychain_add_verified(service, account, blob)

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
