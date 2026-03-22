"""Abstract shell integration."""

from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path


class ShellIntegration(ABC):
    @abstractmethod
    def generate_init_script(self, profiles: list) -> str:
        """Generate the full shell init snippet to source from ~/.bashrc etc."""

    @abstractmethod
    def generate_switch_function(self) -> str:
        """Generate the claudex-switch function body."""

    @abstractmethod
    def generate_profile_alias(self, profile_name: str, config_dir: Path) -> str:
        """Generate a single per-profile launch alias/function."""

    @abstractmethod
    def get_init_files(self) -> list[Path]:
        """Return list of shell init files to install into (e.g. ~/.bashrc)."""

    @abstractmethod
    def generate_env_file(self, config_dir: Path) -> str:
        """Return the content to write to .current_env for this shell."""

    def install(self, profiles: list, init_file: Path | None = None) -> Path:
        """Install the shell integration block into the user's shell init file."""
        from claudex.constants import SHELL_MARKER_BEGIN, SHELL_MARKER_END
        if init_file is None:
            candidates = self.get_init_files()
            init_file = candidates[0] if candidates else Path.home() / ".bashrc"

        init_file.parent.mkdir(parents=True, exist_ok=True)
        existing = init_file.read_text(encoding="utf-8") if init_file.exists() else ""
        # Remove previous block
        existing = _remove_block(existing, SHELL_MARKER_BEGIN, SHELL_MARKER_END)
        block = self.generate_init_script(profiles)
        new_content = existing.rstrip() + f"\n\n{SHELL_MARKER_BEGIN}\n{block}\n{SHELL_MARKER_END}\n"
        init_file.write_text(new_content, encoding="utf-8")
        return init_file

    def is_installed(self, init_file: Path | None = None) -> bool:
        from claudex.constants import SHELL_MARKER_BEGIN
        if init_file is None:
            candidates = self.get_init_files()
            init_file = candidates[0] if candidates else Path.home() / ".bashrc"
        if not init_file.exists():
            return False
        return SHELL_MARKER_BEGIN in init_file.read_text(encoding="utf-8")


def _remove_block(text: str, begin: str, end: str) -> str:
    """Remove a marker-delimited block from a string."""
    lines = text.splitlines(keepends=True)
    result = []
    inside = False
    for line in lines:
        if begin in line:
            inside = True
            continue
        if end in line:
            inside = False
            continue
        if not inside:
            result.append(line)
    return "".join(result)
