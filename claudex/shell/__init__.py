"""Shell integration — generate and install shell aliases/functions."""

from __future__ import annotations

import sys
from claudex.shell.base import ShellIntegration


def get_shell_integration(shell: str = "auto") -> ShellIntegration:
    if shell == "auto":
        shell = _detect_shell()
    if shell in ("bash", "zsh"):
        from claudex.shell.bash import BashIntegration
        return BashIntegration(shell)
    if shell in ("powershell", "pwsh"):
        from claudex.shell.powershell import PowerShellIntegration
        return PowerShellIntegration()
    if shell == "fish":
        from claudex.shell.bash import BashIntegration
        return BashIntegration("fish")
    # Default fallback
    from claudex.shell.bash import BashIntegration
    return BashIntegration("bash")


def _detect_shell() -> str:
    import os
    if sys.platform == "win32":
        return "powershell"
    shell_path = os.environ.get("SHELL", "")
    if "zsh" in shell_path:
        return "zsh"
    if "fish" in shell_path:
        return "fish"
    return "bash"
