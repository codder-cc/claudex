"""File-based credential backend (Linux fallback)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME
from claudex.platform.base import CredentialBackend

CREDS_FILE = CLAUDEX_HOME / ".credentials.json"


class FileCredentialBackend(CredentialBackend):
    def __init__(self) -> None:
        self._path = CREDS_FILE

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            # The store is corrupt. Preserve it (so the tokens aren't silently lost
            # when we next write) instead of returning {} and clobbering it.
            backup = self._path.with_suffix(self._path.suffix + ".corrupt")
            try:
                if not backup.exists():
                    self._path.replace(backup)
            except OSError:
                pass
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Lock down the directory (tokens live here).
        try:
            os.chmod(self._path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass
        # Write to a 0600 temp file in the same dir, then atomically replace, so a
        # crash mid-write can never truncate the existing store, and the plaintext
        # is never momentarily world-readable.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._path))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def store(self, profile: str, key: str, value: str) -> None:
        data = self._load()
        data.setdefault(profile, {})[key] = value
        self._save(data)

    def retrieve(self, profile: str, key: str) -> Optional[str]:
        return self._load().get(profile, {}).get(key)

    def delete(self, profile: str, key: str) -> None:
        data = self._load()
        if profile in data and key in data[profile]:
            del data[profile][key]
            self._save(data)

    def list_keys(self, profile: str) -> list[str]:
        return list(self._load().get(profile, {}).keys())
