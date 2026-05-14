"""Self-update for claudex.

Reads pipx metadata to discover where the current install came from, fast-forwards
the local checkout (if any), and reinstalls via pipx.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()

PIPX_METADATA = Path.home() / ".local" / "pipx" / "venvs" / "claudex" / "pipx_metadata.json"


def _read_install_source() -> Optional[str]:
    if not PIPX_METADATA.exists():
        return None
    try:
        data = json.loads(PIPX_METADATA.read_text(encoding="utf-8"))
    except Exception:
        return None
    return (data.get("main_package") or {}).get("package_or_url")


def _run(cmd: list[str], cwd: Optional[Path] = None) -> int:
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


def run_update(ref: Optional[str] = None) -> None:
    source = _read_install_source()
    if source is None:
        console.print(
            "[red]Could not find pipx install metadata for claudex.[/red]\n"
            "If you installed via pip directly, run [cyan]pip install --upgrade claudex[/cyan]\n"
            "or reinstall from source manually."
        )
        sys.exit(1)

    source_path = Path(source).expanduser()
    local_checkout = source_path.exists() and (source_path / ".git").exists()

    if local_checkout:
        console.print(f"[cyan]Updating local checkout:[/cyan] {source_path}")
        pull_cmd = ["git", "pull", "--ff-only"]
        if ref:
            pull_cmd += ["origin", ref]
        if _run(pull_cmd, cwd=source_path) != 0:
            console.print(
                "[red]git pull --ff-only failed.[/red] "
                "Resolve local changes manually, then rerun [cyan]claudex update[/cyan]."
            )
            sys.exit(1)
        install_spec = str(source_path)
    else:
        install_spec = f"{source}@{ref}" if ref else source
        console.print(f"[cyan]Reinstalling from:[/cyan] {install_spec}")

    if _run(["pipx", "install", "--force", install_spec]) != 0:
        console.print("[red]pipx install failed.[/red]")
        sys.exit(1)

    console.print("[green]✓ claudex updated.[/green]")
    subprocess.call(["claudex", "--version"])
