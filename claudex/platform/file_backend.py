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
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Restrict file permissions to owner only (600)
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
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
