"""Self-update for claudex.

Reads pipx metadata to discover where the current install came from, fast-forwards
the local checkout (if any), and reinstalls via pipx.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()

# A git ref we are willing to interpolate: branch/tag/commit-ish. Deliberately
# excludes anything that could be read as an option (leading '-') or shell/path
# metacharacters, so a hostile --ref can't inject arguments into git/pipx.
_REF_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")


def _pipx_home() -> Path:
    """Resolve pipx's home, honouring PIPX_HOME (matches pipx's own logic)."""
    env = os.environ.get("PIPX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "pipx"


def _pipx_metadata() -> Path:
    return _pipx_home() / "venvs" / "claudex" / "pipx_metadata.json"


def _read_install_source() -> Optional[str]:
    meta = _pipx_metadata()
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None
    return (data.get("main_package") or {}).get("package_or_url")


def _run(cmd: list[str], cwd: Optional[Path] = None) -> int:
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


def run_update(ref: Optional[str] = None) -> None:
    if ref is not None and not _REF_RE.match(ref):
        console.print(
            f"[red]Invalid --ref {ref!r}.[/red] Use only letters, digits, '.', '_', '/', '-' "
            "(a branch, tag, or commit)."
        )
        sys.exit(1)

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
            # `--` terminates option parsing so a ref can never be read as a flag.
            pull_cmd += ["origin", "--", ref]
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

    # Use pipx from the same environment that is running us when possible, rather
    # than whatever bare `pipx`/`claudex` happens to be first on PATH.
    pipx_cmd = [sys.executable, "-m", "pipx"] if _has_pipx_module() else ["pipx"]
    if _run([*pipx_cmd, "install", "--force", install_spec]) != 0:
        console.print("[red]pipx install failed.[/red]")
        sys.exit(1)

    # Verify the freshly-installed binary actually runs before declaring success.
    if subprocess.call(["claudex", "--version"]) != 0:
        console.print("[yellow]claudex was reinstalled but 'claudex --version' did not run cleanly.[/yellow]")
        sys.exit(1)
    console.print("[green]✓ claudex updated.[/green]")


def _has_pipx_module() -> bool:
    import importlib.util
    return importlib.util.find_spec("pipx") is not None
