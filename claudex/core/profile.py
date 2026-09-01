"""Profile management — create, delete, list, switch profiles."""

from __future__ import annotations

import os
import re
import sys
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from claudex.constants import (
    CLAUDEX_HOME, PROFILES_DIR, SHARED_DIR, ACTIVE_PROFILE_FILE,
    CURRENT_ENV_BASH, CURRENT_ENV_PWSH, CLAUDE_CONFIG_DIR_ENV,
    SHAREABLE_RESOURCES, IS_WINDOWS,
)
from claudex.exceptions import ProfileNotFoundError, ProfileExistsError, ClaudexError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-reuse-import]

import tomli_w

# A profile name becomes a single filesystem path component AND is interpolated
# into generated shell snippets, so it must be a strict, shell-safe slug. This
# blocks path traversal (``/`` ``\`` ``..``) and shell metacharacters (``;`` ``$``
# ``` ` ``` etc.) that would otherwise execute when ~/.bashrc is sourced.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def validate_profile_name(name: str) -> str:
    """Return *name* if it is a safe profile slug, else raise ClaudexError."""
    if not name or not _NAME_RE.match(name) or name in (".", "..") or len(name) > 64:
        raise ClaudexError(
            f"Invalid profile name {name!r}. Use letters, digits, '.', '_' or '-' "
            "(must start with a letter, digit or underscore; max 64 chars)."
        )
    return name


def _write_toml(data: dict, path: Path) -> None:
    # tomli_w handles escaping of quotes/backslashes/control chars correctly,
    # which a hand-rolled serialiser does not.
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


@dataclass
class Profile:
    name: str
    config_dir: Path
    created_at: datetime = field(default_factory=datetime.now)
    last_used: Optional[datetime] = None
    aliases: list[str] = field(default_factory=list)
    email: str = ""
    auth_type: Literal["oauth", "api_key", "none"] = "none"
    shared_resources: list[str] = field(default_factory=list)
    color: str = "cyan"
    notes: str = ""

    @classmethod
    def from_toml(cls, path: Path) -> "Profile":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # Always trust the on-disk location: a stored config_dir can be alien (a pulled
        # bundle carries the source machine's home path) and would point us at a
        # directory we can't even write to.
        data["config_dir"] = path.parent
        if "created_at" in data and isinstance(data["created_at"], str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if "last_used" in data and isinstance(data["last_used"], str) and data["last_used"]:
            data["last_used"] = datetime.fromisoformat(data["last_used"])
        else:
            data["last_used"] = None
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        data = {
            "name": self.name,
            "config_dir": self.config_dir.as_posix(),
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat() if self.last_used else "",
            "aliases": self.aliases,
            "email": self.email,
            "auth_type": self.auth_type,
            "shared_resources": self.shared_resources,
            "color": self.color,
            "notes": self.notes,
        }
        _write_toml(data, self.config_dir / "profile.toml")


class ProfileManager:
    def __init__(self) -> None:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        SHARED_DIR.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        email: str = "",
        aliases: list[str] | None = None,
        color: str = "cyan",
        notes: str = "",
    ) -> Profile:
        validate_profile_name(name)
        if self.exists(name):
            raise ProfileExistsError(name)
        config_dir = PROFILES_DIR / name
        config_dir.mkdir(parents=True, exist_ok=True)

        profile = Profile(
            name=name,
            config_dir=config_dir,
            aliases=aliases or [f"claude-{name}"],
            email=email,
            color=color,
            notes=notes,
        )
        profile.save()
        return profile

    def get(self, name: str) -> Profile:
        toml_path = PROFILES_DIR / name / "profile.toml"
        if not toml_path.exists():
            raise ProfileNotFoundError(name)
        return Profile.from_toml(toml_path)

    def exists(self, name: str) -> bool:
        return (PROFILES_DIR / name / "profile.toml").exists()

    def list(self) -> list[Profile]:
        profiles = []
        for entry in sorted(PROFILES_DIR.iterdir()):
            toml = entry / "profile.toml"
            if toml.exists():
                try:
                    profiles.append(Profile.from_toml(toml))
                except Exception:
                    pass
        return profiles

    def delete(self, name: str, purge_history: bool = False) -> None:
        profile = self.get(name)
        if purge_history:
            shutil.rmtree(profile.config_dir, ignore_errors=True)
        else:
            # Keep history but remove profile marker
            toml = profile.config_dir / "profile.toml"
            toml.unlink(missing_ok=True)
        active = self.get_active()
        if active == name:
            ACTIVE_PROFILE_FILE.unlink(missing_ok=True)

    def get_active(self) -> str:
        if ACTIVE_PROFILE_FILE.exists():
            return ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
        # Fallback: check env var
        return os.environ.get(CLAUDE_CONFIG_DIR_ENV, "")

    def set_active(self, name: str) -> None:
        # Validate so a malicious value (e.g. from a repo-local .claudeprofile fed
        # through the auto-switch hook) can never reach the filesystem layer.
        validate_profile_name(name)
        profile = self.get(name)
        ACTIVE_PROFILE_FILE.write_text(name, encoding="utf-8")
        # Update last_used
        profile.last_used = datetime.now()
        profile.save()
        # Write env files for shell sourcing
        self._write_env_files(profile.config_dir)

    def _write_env_files(self, config_dir: Path) -> None:
        """Write .current_env and .current_env.ps1 for shell functions to source."""
        import shlex
        CLAUDEX_HOME.mkdir(parents=True, exist_ok=True)
        # Bash / Zsh — POSIX-quote the path in case home contains spaces/quotes.
        CURRENT_ENV_BASH.write_text(
            f'export {CLAUDE_CONFIG_DIR_ENV}={shlex.quote(str(config_dir))}\n',
            encoding="utf-8",
        )
        # PowerShell — single-quote (no interpolation; double embedded quotes).
        ps_path = "'" + str(config_dir).replace("'", "''") + "'"
        CURRENT_ENV_PWSH.write_text(
            f'$env:{CLAUDE_CONFIG_DIR_ENV} = {ps_path}\n',
            encoding="utf-8",
        )

    def get_config_dir(self, name: str) -> Path:
        return self.get(name).config_dir

    def rename(self, old_name: str, new_name: str) -> Profile:
        validate_profile_name(new_name)
        if not self.exists(old_name):
            raise ProfileNotFoundError(old_name)
        if self.exists(new_name):
            raise ProfileExistsError(new_name)
        old_dir = PROFILES_DIR / old_name
        new_dir = PROFILES_DIR / new_name
        old_dir.rename(new_dir)
        profile = Profile.from_toml(new_dir / "profile.toml")
        profile.name = new_name
        profile.config_dir = new_dir
        profile.save()
        # If the renamed profile was the active one, repoint the active marker
        # and the sourced env files so switch/resume keep working.
        if self.get_active() == old_name:
            self.set_active(new_name)
        return profile

    def set_resource_isolation(
        self,
        name: str,
        resource: str,
        shared: bool,
    ) -> None:
        """Toggle a resource between isolated and shared (symlink) mode."""
        profile = self.get(name)
        target = profile.config_dir / resource
        shared_src = SHARED_DIR / resource

        if shared:
            # Create symlink in profile dir pointing to shared
            if target.exists() and not target.is_symlink():
                # Seed the shared copy from this profile's real data if it doesn't
                # exist yet, so toggling never silently discards the user's files.
                if not shared_src.exists():
                    self._copy_path(target, shared_src)
                self._remove_path(target)
            if not target.exists():
                self._create_symlink(shared_src, target)
            if resource not in profile.shared_resources:
                profile.shared_resources.append(resource)
        else:
            # Break symlink — copy shared into profile
            if target.is_symlink():
                link_target = target.resolve()
                target.unlink()
                if link_target.exists():
                    self._copy_path(link_target, target)
            if resource in profile.shared_resources:
                profile.shared_resources.remove(resource)

        profile.save()

    @staticmethod
    def _copy_path(src: Path, dst: Path) -> None:
        """Copy a file or directory tree from *src* to *dst*."""
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))

    @staticmethod
    def _remove_path(path: Path) -> None:
        """Remove a file or directory tree."""
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _create_symlink(self, src: Path, dst: Path) -> None:
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if IS_WINDOWS:
            try:
                dst.symlink_to(src, target_is_directory=src.is_dir())
            except OSError:
                # Fallback: copy (Developer Mode may not be enabled)
                if src.is_dir():
                    shutil.copytree(str(src), str(dst))
                elif src.exists():
                    shutil.copy2(str(src), str(dst))
        else:
            dst.symlink_to(src)
