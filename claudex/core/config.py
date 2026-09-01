"""Global claudex configuration (config.toml)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from claudex.constants import GLOBAL_CONFIG_FILE, CLAUDEX_HOME, PROFILES_DIR, SHARED_DIR

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-reuse-import]

import tomli_w


def _write_toml(data: dict, path: Path) -> None:
    """Serialise config to TOML with correct escaping (via tomli_w)."""
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


_DEFAULT_CONFIG: dict[str, Any] = {
    "default_profile": "",
    "shell": "auto",          # auto | bash | zsh | fish | powershell
    "theme": "dark",          # dark | light
    "auto_switch": True,      # .claudeprofile file detection
    "resume_strategy": "env", # env | direct (--resume flag)
    "tui_refresh_seconds": 5,
    "sharing_endpoint": "",   # URL of a claudex-sharing server (e.g. https://codder.cc)
}


class GlobalConfig:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._path = GLOBAL_CONFIG_FILE

    def load(self) -> "GlobalConfig":
        if self._path.exists():
            try:
                with open(self._path, "rb") as f:
                    loaded = tomllib.load(f)
            except (tomllib.TOMLDecodeError, OSError):
                loaded = {}
            # Merge over defaults so newly-added keys are always present and a
            # partial/older file doesn't get truncated when re-saved.
            self._data = {**_DEFAULT_CONFIG, **loaded}
        else:
            self._data = dict(_DEFAULT_CONFIG)
        return self

    def save(self) -> None:
        CLAUDEX_HOME.mkdir(parents=True, exist_ok=True)
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        _write_toml(self._data, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, _DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    @property
    def default_profile(self) -> str:
        return self.get("default_profile", "")

    @default_profile.setter
    def default_profile(self, name: str) -> None:
        self.set("default_profile", name)

    @property
    def shell(self) -> str:
        return self.get("shell", "auto")

    @property
    def theme(self) -> str:
        return self.get("theme", "dark")

    @property
    def auto_switch(self) -> bool:
        return bool(self.get("auto_switch", True))

    @property
    def resume_strategy(self) -> str:
        return self.get("resume_strategy", "env")

    @property
    def sharing_endpoint(self) -> str:
        return self.get("sharing_endpoint", "")

    @sharing_endpoint.setter
    def sharing_endpoint(self, url: str) -> None:
        self.set("sharing_endpoint", url.rstrip("/"))


def load_config() -> GlobalConfig:
    return GlobalConfig().load()
