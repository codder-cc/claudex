"""claudex doctor — diagnose installation and config issues."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from claudex.constants import (
    CLAUDEX_HOME, PROFILES_DIR, CLAUDE_CONFIG_DIR_ENV,
    CLAUDE_BIN, SHELL_MARKER_BEGIN,
)

console = Console()


def _check(label: str, ok: bool, detail: str = "", fix: str = "") -> tuple[str, str, str, str]:
    status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
    return status, label, detail, fix


def run_doctor() -> None:
    console.print("\n[bold cyan]claudex doctor[/bold cyan] — Diagnostics\n")
    results = []

    # 1. Claude binary
    claude_path = shutil.which(CLAUDE_BIN)
    results.append(_check(
        "claude binary",
        claude_path is not None,
        claude_path or "not found",
        "Install Claude Code from https://claude.ai/code" if not claude_path else "",
    ))

    # 2. CLAUDEX_HOME
    results.append(_check(
        "~/.claudex directory",
        CLAUDEX_HOME.exists(),
        str(CLAUDEX_HOME),
        "Run 'claudex list' to initialise" if not CLAUDEX_HOME.exists() else "",
    ))

    # 3. Profiles
    profile_count = 0
    if PROFILES_DIR.exists():
        profile_count = sum(1 for p in PROFILES_DIR.iterdir() if (p / "profile.toml").exists())
    results.append(_check(
        "profiles defined",
        profile_count > 0,
        f"{profile_count} profile(s)",
        "Run 'claudex new <name>' to create your first profile" if profile_count == 0 else "",
    ))

    # 4. Credential backend
    try:
        from claudex.platform import get_credential_backend
        backend = get_credential_backend()
        backend_name = type(backend).__name__
        results.append(_check("credential backend", True, backend_name))
    except Exception as e:
        results.append(_check("credential backend", False, str(e), "Check keyring/dependencies"))

    # 5. Shell integration
    shell_installed = False
    shell_files_checked = []
    home = Path.home()
    for rc in [home / ".bashrc", home / ".zshrc", home / ".bash_profile"]:
        if rc.exists() and SHELL_MARKER_BEGIN in rc.read_text(encoding="utf-8", errors="ignore"):
            shell_installed = True
            shell_files_checked.append(str(rc))
    # PowerShell
    ps_profile = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    if ps_profile.exists() and SHELL_MARKER_BEGIN in ps_profile.read_text(encoding="utf-8", errors="ignore"):
        shell_installed = True
        shell_files_checked.append(str(ps_profile))
    results.append(_check(
        "shell integration installed",
        shell_installed,
        ", ".join(shell_files_checked) or "not found",
        "Run 'claudex shell setup' to install" if not shell_installed else "",
    ))

    # 6. CLAUDE_CONFIG_DIR env var
    current_dir = os.environ.get(CLAUDE_CONFIG_DIR_ENV, "")
    results.append(_check(
        f"{CLAUDE_CONFIG_DIR_ENV} set",
        bool(current_dir),
        current_dir or "not set",
        "Run 'claudex switch <name>' or 'claudex-switch <name>' from your shell" if not current_dir else "",
    ))

    # 7. Active profile
    from claudex.constants import ACTIVE_PROFILE_FILE
    active = ""
    if ACTIVE_PROFILE_FILE.exists():
        try:
            active = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            active = ""
    results.append(_check(
        "active profile",
        bool(active),
        active or "none",
        "Run 'claudex switch <name>'" if not active else "",
    ))

    # 8. Parse a session file
    session_ok = False
    session_detail = "no sessions found"
    found_any = False
    if PROFILES_DIR.exists():
        from claudex.history.parser import parse_session_file
        for profile_dir in PROFILES_DIR.iterdir():
            # Match the real parser layout (projects/<encoded>/<uuid>.jsonl) rather
            # than an unbounded rglob, so PASS/FAIL reflects what's actually read.
            for jsonl in (profile_dir / "projects").glob("*/*.jsonl"):
                found_any = True
                try:
                    s = parse_session_file(jsonl, profile_dir.name)
                    if s:
                        session_ok = True
                        session_detail = f"parsed OK: {jsonl.name}"
                        break
                except Exception as e:
                    session_detail = f"parse error: {e}"
            if session_ok:
                break
    # No sessions at all isn't a failure — only a real parse failure is.
    if not found_any:
        session_ok = True
        session_detail = "no sessions yet"
    results.append(_check("JSONL session parsing", session_ok, session_detail,
                           "Sessions will appear after running claude in a profile" if not session_ok else ""))

    # 9. macOS Keychain consistency: claudex reads .credentials.json/backend, but
    #    Claude Code reads the Keychain. If they disagree, claudex shows authed
    #    while Claude prompts to log in (the cross-machine share symptom).
    from claudex.constants import IS_MACOS
    if IS_MACOS and PROFILES_DIR.exists():
        from claudex.core.auth import AuthManager
        from claudex.core.profile import ProfileManager
        am = AuthManager()
        mismatched = []
        for p in ProfileManager().list():
            try:
                st = am.get_status(p.name, p.config_dir)
                if st.auth_type != "oauth":
                    continue
                if am.macos_keychain_token_present(p.config_dir) is False:
                    mismatched.append(p.name)
            except Exception:
                continue
        results.append(_check(
            "macOS Keychain matches claudex auth",
            not mismatched,
            "all profiles consistent" if not mismatched
            else f"missing Keychain token: {', '.join(mismatched)}",
            "Run 'claudex auth import-current <name>' or 'claudex auth relogin <name>'"
            if mismatched else "",
        ))

    # Print results table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=8)
    table.add_column("Check", width=30)
    table.add_column("Detail", width=40)
    table.add_column("Fix", width=50)
    for row in results:
        table.add_row(*row)
    console.print(table)

    fails = sum(1 for r in results if "FAIL" in r[0])
    if fails == 0:
        console.print("\n[green bold]All checks passed![/green bold]\n")
    else:
        console.print(f"\n[red bold]{fails} check(s) failed.[/red bold] See 'Fix' column above.\n")
    # Non-zero exit so `claudex doctor` is usable as a CI / scripting health gate.
    import sys
    sys.exit(1 if fails else 0)
