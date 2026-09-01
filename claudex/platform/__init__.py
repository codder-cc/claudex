"""Platform-specific credential backend factory."""

from __future__ import annotations

from claudex.constants import IS_WINDOWS, IS_MACOS
from claudex.platform.base import CredentialBackend


def get_credential_backend() -> CredentialBackend:
    """Return the best available credential backend for this platform.

    Selection is non-destructive: we inspect the active keyring backend's type
    and priority rather than writing a probe entry. The old behaviour ran a
    write round-trip (`backend.test()`) on every invocation, which littered the
    Keychain with `__test__` entries and — when the login keychain was missing or
    locked — popped a blocking "Keychain Not Found" dialog. Real operational
    failures are now surfaced where credentials are actually used (the CLI/TUI
    command handlers catch them), and `CredentialBackend.test()` remains for
    explicit diagnostics like `claudex doctor`.
    """
    try:
        import keyring
        kr = keyring.get_keyring()
        name = type(kr).__name__
        # The null/fail backends report priority <= 0 and raise on use; a real
        # OS-backed store (macOS Keychain, Windows Credential Locker, SecretService,
        # KWallet) reports a positive priority.
        is_fail = "fail" in name.lower() or "null" in name.lower()
        priority = getattr(kr, "priority", 0) or 0
        if not is_fail and priority >= 1:
            from claudex.platform.keyring_backend import KeyringBackend
            return KeyringBackend()
    except Exception:
        pass

    from claudex.platform.file_backend import FileCredentialBackend
    return FileCredentialBackend()
