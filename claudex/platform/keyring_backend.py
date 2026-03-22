"""Keyring-based credential backend (Windows Credential Manager / macOS Keychain)."""

from __future__ import annotations
from typing import Optional

import keyring

from claudex.constants import CREDENTIAL_SERVICE
from claudex.platform.base import CredentialBackend


class KeyringBackend(CredentialBackend):
    def _key(self, profile: str, key: str) -> str:
        return f"{profile}:{key}"

    def store(self, profile: str, key: str, value: str) -> None:
        keyring.set_password(CREDENTIAL_SERVICE, self._key(profile, key), value)

    def retrieve(self, profile: str, key: str) -> Optional[str]:
        return keyring.get_password(CREDENTIAL_SERVICE, self._key(profile, key))

    def delete(self, profile: str, key: str) -> None:
        try:
            keyring.delete_password(CREDENTIAL_SERVICE, self._key(profile, key))
        except keyring.errors.PasswordDeleteError:
            pass

    def list_keys(self, profile: str) -> list[str]:
        # keyring has no list API; return known key types
        known = ["oauth_token", "refresh_token", "api_key", "email", "expires_at"]
        return [k for k in known if self.retrieve(profile, k) is not None]
