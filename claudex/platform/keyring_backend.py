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
            pass  # key not present — already gone
        except keyring.errors.KeyringError:
            # A backend hiccup (locked keyring, transient error) must not abort a
            # multi-key revoke partway through and leave credentials half-deleted.
            pass

    def list_keys(self, profile: str) -> list[str]:
        # keyring has no list API; probe the full set of keys claudex ever stores
        # (must stay in sync with AuthManager.revoke so deletes don't orphan keys).
        known = ["oauth_token", "refresh_token", "api_key", "auth_type",
                 "email", "expires_at"]
        return [k for k in known if self.retrieve(profile, k) is not None]
