"""Auth manager — token lifecycle, OAuth and API key management."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from claudex.constants import CLAUDE_CONFIG_DIR_ENV, CLAUDE_BIN
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
            return "never" if self.auth_type == "api_key" else "unknown"
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
        return "none", "", None, ""

    def _claude_available(self) -> bool:
        import shutil
        return shutil.which(CLAUDE_BIN) is not None
