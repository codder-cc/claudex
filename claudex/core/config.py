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

try:
    import tomllib as _tomllib_write  # noqa: F401 — only for type check
except ImportError:
    pass


def _write_toml(data: dict, path: Path) -> None:
    """Simple TOML serialiser (no external write dependency needed for our schema)."""
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"\n[{key}]")
            for k, v in value.items():
                lines.append(f"{k} = {_toml_value(v)}")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    return str(v)


_DEFAULT_CONFIG: dict[str, Any] = {
    "default_profile": "",
    "shell": "auto",          # auto | bash | zsh | fish | powershell
    "theme": "dark",          # dark | light
    "auto_switch": True,      # .claudeprofile file detection
    "resume_strategy": "env", # env | direct (--resume flag)
    "tui_refresh_seconds": 5,
}


class GlobalConfig:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._path = GLOBAL_CONFIG_FILE

    def load(self) -> "GlobalConfig":
        if self._path.exists():
            with open(self._path, "rb") as f:
                self._data = tomllib.load(f)
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


def load_config() -> GlobalConfig:
    return GlobalConfig().load()
