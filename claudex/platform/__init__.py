"""Platform-specific credential backend factory."""

from __future__ import annotations

from claudex.constants import IS_WINDOWS, IS_MACOS
from claudex.platform.base import CredentialBackend


def get_credential_backend() -> CredentialBackend:
    """Return the best available credential backend for this platform."""
    if IS_WINDOWS or IS_MACOS:
        try:
            from claudex.platform.keyring_backend import KeyringBackend
            backend = KeyringBackend()
            backend.test()
            return backend
        except Exception:
            pass

    # Linux or keyring fallback
    try:
        import keyring
        kr = keyring.get_keyring()
        if "SecretService" in type(kr).__name__ or "Kwallet" in type(kr).__name__:
            from claudex.platform.keyring_backend import KeyringBackend
            return KeyringBackend()
    except Exception:
        pass

    from claudex.platform.file_backend import FileCredentialBackend
    return FileCredentialBackend()
