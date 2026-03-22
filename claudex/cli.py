"""
claudex — Claude Code cross-platform profile manager and session switcher.

Usage:
  claudex                               Launch TUI dashboard (press ? for help)
  claudex list                          List all profiles + auth status

  -- Profiles --
  claudex new <name> [--email]          Create a new profile
  claudex switch <name>                 Set active profile (writes env file to source)
  claudex use <name>                    Launch claude with a profile (one-shot)
  claudex delete <name>                 Delete a profile
  claudex rename <old> <new>            Rename a profile

  -- Authentication --
  claudex auth import-current <name>    Import your CURRENT claude login (no re-auth!)
                                        Use this if you are already logged in to Claude
  claudex auth add <name>               Fresh OAuth login (opens browser via claude /login)
  claudex auth key <name>               Add a console.anthropic.com API key (sk-ant-api03-...)
  claudex auth status                   Show token type + expiry for all profiles
  claudex auth revoke <name>            Clear stored credentials

  -- Sessions --
  claudex session list [name]           List recent sessions
  claudex session resume [name]         Resume last session for a profile
  claudex session migrate <id>          Move a session between profiles

  -- History --
  claudex history                       Open history browser (TUI)
  claudex search <query>                Search sessions by title/project

  -- Shell --
  claudex shell setup                   Install shell aliases + auto-switch hook
  claudex shell hook                    Print shell snippet (for manual inclusion)

  -- Other --
  claudex doctor                        Diagnose installation issues
  claudex export <name>                 Export profile to .tar.gz
  claudex import <file>                 Import profile from .tar.gz
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

# Force UTF-8 output on Windows to allow Unicode characters
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from claudex.constants import (
    CLAUDE_BIN, CLAUDE_CONFIG_DIR_ENV, CLAUDEX_HOME, PROFILES_DIR,
)
from claudex.core.auth import AuthManager
from claudex.core.config import load_config
from claudex.core.profile import ProfileManager
from claudex.exceptions import ClaudexError

console = Console(force_terminal=True, highlight=True)


def _pm() -> ProfileManager:
    return ProfileManager()


def _auth() -> AuthManager:
    return AuthManager()


def _seed_claude_json(config_dir: Path) -> None:
    """Seed .claude.json in profile dir so interactive Claude skips the auth selector.

    Claude looks for .claude.json inside CLAUDE_CONFIG_DIR on startup to find the
    oauthAccount entry. Without it, interactive Claude shows the auth-type selection
    prompt even when .credentials.json is present (non-interactive -p works fine).
    We copy the essential fields from ~/.claude.json (home-level) to bootstrap it.
    """
    import json as _json

    home_claude_json = Path.home() / ".claude.json"
    dest = config_dir / ".claude.json"

    # Fields that tell interactive Claude this profile is initialized
    SEED_KEYS = {
        "oauthAccount", "userID", "hasCompletedOnboarding", "lastOnboardingVersion",
        "installMethod", "autoUpdates",
    }

    seed_data: dict = {}
    if home_claude_json.exists():
        try:
            home_data = _json.loads(home_claude_json.read_text(encoding="utf-8"))
            seed_data = {k: v for k, v in home_data.items() if k in SEED_KEYS}
        except Exception:
            pass

    if not seed_data:
        return  # Nothing useful to seed

    # Merge into existing .claude.json if present (preserve Claude-written state)
    existing: dict = {}
    if dest.exists():
        try:
            existing = _json.loads(dest.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    merged = {**seed_data, **existing}  # existing takes precedence
    dest.write_text(_json.dumps(merged, indent=2) + "\n", encoding="utf-8")


# ─── Root command ─────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="claudex")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """claudex — Claude Code cross-platform profile manager."""
    if ctx.invoked_subcommand is None:
        from claudex.tui.app import run_app
        run_app()


# ─── Profile commands ──────────────────────────────────────────────────────────

@cli.command("list")
def list_profiles() -> None:
    """List all profiles."""
    pm = _pm()
    am = _auth()
    profiles = pm.list()
    active = pm.get_active()

    if not profiles:
        console.print("[yellow]No profiles. Run:[/yellow] claudex new <name>")
        return

    table = Table(title="Profiles", show_header=True, header_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("Name", style="bold")
    table.add_column("Auth")
    table.add_column("Email")
    table.add_column("Last Used")
    table.add_column("Sessions")
    table.add_column("Expires")

    for p in profiles:
        is_active = active == p.name or str(p.config_dir) in active
        marker = "▶" if is_active else " "
        try:
            status = am.get_status(p.name, p.config_dir)
            auth = f"[green]{status.auth_type}[/green]" if status.auth_type != "none" else "[red]none[/red]"
            expires = status.expires_in_human
            email = status.email or p.email or "—"
        except Exception:
            auth = "[red]none[/red]"
            expires = "—"
            email = p.email or "—"

        sessions = sum(1 for _ in (p.config_dir / "projects").rglob("*.jsonl")) if (p.config_dir / "projects").exists() else 0
        last = "never"
        if p.last_used:
            try:
                import humanize
                last = humanize.naturaltime(p.last_used)
            except Exception:
                from datetime import datetime
                delta = datetime.now() - p.last_used
                last = f"{int(delta.total_seconds() // 3600)}h ago"

        table.add_row(marker, p.name, auth, email, last, str(sessions), expires)

    console.print(table)


@cli.command("new")
@click.argument("name")
@click.option("--email", "-e", default="", help="Email address for this account")
@click.option("--alias", "-a", multiple=True, help="Shell aliases (repeatable)")
@click.option("--color", default="cyan", help="TUI accent color")
@click.option("--notes", default="", help="Notes about this profile")
def new_profile(name: str, email: str, alias: tuple, color: str, notes: str) -> None:
    """Create a new profile."""
    try:
        pm = _pm()
        aliases = list(alias) or [f"claude-{name}"]
        profile = pm.create(name, email=email, aliases=aliases, color=color, notes=notes)
        console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] created at {profile.config_dir}")
        console.print(f"  Next: [cyan]claudex auth add {name}[/cyan] to authenticate")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command("switch")
@click.argument("name")
def switch_profile(name: str) -> None:
    """Set the active profile (writes env files for shell to source)."""
    try:
        pm = _pm()
        pm.set_active(name)
        from claudex.constants import CURRENT_ENV_BASH, CURRENT_ENV_PWSH
        console.print(f"[green]✓[/green] Active profile set to [bold]{name}[/bold]")
        console.print()
        if sys.platform == "win32":
            console.print(f"  Run in PowerShell:  [cyan]. '{CURRENT_ENV_PWSH}'[/cyan]")
        else:
            console.print(f"  Run in your shell:  [cyan]source {CURRENT_ENV_BASH}[/cyan]")
        console.print(f"  Or use the alias:   [cyan]claudex-switch {name}[/cyan]")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command("use")
@click.argument("name")
@click.argument("claude_args", nargs=-1)
def use_profile(name: str, claude_args: tuple) -> None:
    """Launch claude with a specific profile (one-shot, does not persist)."""
    try:
        pm = _pm()
        am = _auth()
        profile = pm.get(name)
        env = {**os.environ, **am.get_env_for_profile(name, profile.config_dir)}
        cmd = [CLAUDE_BIN] + list(claude_args)
        if sys.platform != "win32":
            os.execvpe(cmd[0], cmd, env)
        else:
            subprocess.run(cmd, env=env)
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except FileNotFoundError:
        console.print(f"[red]Error:[/red] Claude CLI not found. Install from https://claude.ai/code")
        sys.exit(1)


@cli.command("delete")
@click.argument("name")
@click.option("--purge", is_flag=True, help="Also delete all history and config files")
@click.confirmation_option(prompt="Are you sure you want to delete this profile?")
def delete_profile(name: str, purge: bool) -> None:
    """Delete a profile."""
    try:
        _pm().delete(name, purge_history=purge)
        console.print(f"[green]✓[/green] Profile '{name}' deleted.")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def rename_profile(old_name: str, new_name: str) -> None:
    """Rename a profile."""
    try:
        _pm().rename(old_name, new_name)
        console.print(f"[green]✓[/green] Renamed '{old_name}' → '{new_name}'")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command("export")
@click.argument("name")
@click.option("--output", "-o", default=None, help="Output path (default: <name>.tar.gz)")
def export_profile(name: str, output: Optional[str]) -> None:
    """Export a profile directory (without credentials)."""
    try:
        pm = _pm()
        profile = pm.get(name)
        out_path = Path(output) if output else Path(f"{name}.tar.gz")
        with tarfile.open(out_path, "w:gz") as tar:
            for item in profile.config_dir.rglob("*"):
                # Exclude credential files
                if item.name in (".credentials.json", "credentials.json"):
                    continue
                tar.add(item, arcname=item.relative_to(profile.config_dir.parent))
        console.print(f"[green]✓[/green] Exported to {out_path}")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command("import")
@click.argument("file", type=click.Path(exists=True))
def import_profile(file: str) -> None:
    """Import a profile from a tar.gz export."""
    try:
        src = Path(file)
        with tarfile.open(src, "r:gz") as tar:
            tar.extractall(path=PROFILES_DIR.parent)
        console.print(f"[green]✓[/green] Imported from {src}")
        console.print("  Run 'claudex list' to see the imported profile.")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ─── Auth commands ─────────────────────────────────────────────────────────────

@cli.group("auth")
def auth_group() -> None:
    """Authentication management."""


@auth_group.command("add")
@click.argument("name")
def auth_add(name: str) -> None:
    """Launch Claude OAuth login for a profile."""
    try:
        pm = _pm()
        am = _auth()
        profile = pm.get(name)
        console.print(f"[cyan]Launching OAuth login for profile [bold]{name}[/bold]...[/cyan]")
        console.print(f"  Using CLAUDE_CONFIG_DIR={profile.config_dir}")
        am.add_account_oauth(name, profile.config_dir)
        profile.auth_type = "oauth"
        profile.save()
        _seed_claude_json(profile.config_dir)
        console.print(f"[green]✓[/green] Auth configured for '{name}'")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@auth_group.command("key")
@click.argument("name")
@click.option("--key", "-k", prompt="API key (sk-ant-...)", hide_input=True)
def auth_key(name: str, key: str) -> None:
    """Add an Anthropic API key for a profile."""
    try:
        pm = _pm()
        am = _auth()
        am.add_api_key(name, key)
        profile = pm.get(name)
        profile.auth_type = "api_key"
        profile.save()
        console.print(f"[green]✓[/green] API key stored for '{name}'")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@auth_group.command("status")
def auth_status() -> None:
    """Show auth status for all profiles."""
    pm = _pm()
    am = _auth()
    profiles = pm.list()
    if not profiles:
        console.print("[yellow]No profiles found.[/yellow]")
        return
    table = Table(title="Auth Status", header_style="bold cyan")
    table.add_column("Profile", style="bold")
    table.add_column("Type")
    table.add_column("Email")
    table.add_column("Status")
    table.add_column("Expires")
    table.add_column("Token Preview")
    for p in profiles:
        try:
            s = am.get_status(p.name, p.config_dir)
            color = "green" if s.auth_type != "none" and not s.is_expired else \
                    "red" if s.is_expired else "yellow"
            status = "[red]EXPIRED[/red]" if s.is_expired else \
                     f"[{color}]active[/{color}]" if s.auth_type != "none" else "[dim]none[/dim]"
            table.add_row(p.name, s.auth_type, s.email, status, s.expires_in_human, s.raw_token_preview)
        except Exception as e:
            table.add_row(p.name, "—", "—", f"[red]error: {e}[/red]", "—", "—")
    console.print(table)


@auth_group.command("import-current")
@click.argument("name")
def auth_import_current(name: str) -> None:
    """Import your active Claude login — no re-authentication needed.

    Copies the OAuth token from ~/.claude/.credentials.json (where Claude
    stores your current subscription login) into the named profile.

    Use this when you are already logged in to Claude Code and just want
    to reuse that session in a claudex profile without logging in again.

    Example:
      claudex new work
      claudex auth import-current work
      claudex use work
    """
    try:
        pm = _pm()
        am = _auth()
        # Auto-create the profile if it doesn't exist yet
        if not pm.exists(name):
            profile = pm.create(name)
            console.print(f"[dim]Created profile '{name}'[/dim]")
        else:
            profile = pm.get(name)

        # Claude stores the active session in ~/.claude/.credentials.json (Win/Linux)
        # or in the default CLAUDE_CONFIG_DIR if that env var was set
        default_claude = Path.home() / ".claude"
        candidates = [
            default_claude / ".credentials.json",
            default_claude / "credentials.json",
            Path.home() / ".claude.json",
        ]
        # Also check if CLAUDE_CONFIG_DIR is set (user may already be in a profile)
        existing_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
        if existing_dir:
            candidates.insert(0, Path(existing_dir) / ".credentials.json")

        imported = False
        for cred_file in candidates:
            if cred_file.exists():
                import shutil as _shutil
                dest = profile.config_dir / ".credentials.json"
                _shutil.copy2(str(cred_file), str(dest))
                # Also import into our credential backend (best-effort — keyring may fail)
                try:
                    am._import_claude_credentials(name, profile.config_dir)
                except Exception:
                    pass
                # Update profile auth type (falls back to reading credentials file directly)
                status = am.get_status(name, profile.config_dir)
                profile.auth_type = status.auth_type if status.auth_type != "none" else "oauth"
                profile.email = profile.email or status.email
                profile.save()
                # Seed .claude.json in profile dir so interactive Claude skips the auth selector
                _seed_claude_json(profile.config_dir)
                console.print(f"[green]✓[/green] Imported session from {cred_file}")
                console.print(f"  Auth type: {status.auth_type}")
                if status.email:
                    console.print(f"  Email: {status.email}")
                console.print(f"  Expires: {status.expires_in_human}")
                imported = True
                break

        if not imported:
            console.print("[yellow]No active Claude session found.[/yellow]")
            console.print(f"  Checked: {', '.join(str(c) for c in candidates)}")
            console.print(f"  Run: [cyan]claudex auth add {name}[/cyan] to log in fresh")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@auth_group.command("revoke")
@click.argument("name")
@click.confirmation_option(prompt="Revoke auth credentials for this profile?")
def auth_revoke(name: str) -> None:
    """Revoke/clear stored credentials for a profile."""
    _auth().revoke(name)
    console.print(f"[green]✓[/green] Credentials cleared for '{name}'")


# ─── Session commands ──────────────────────────────────────────────────────────

@cli.group("session")
def session_group() -> None:
    """Session management."""


@session_group.command("list")
@click.argument("name", required=False)
@click.option("--limit", "-n", default=20, help="Number of sessions to show")
def session_list(name: Optional[str], limit: int) -> None:
    """List sessions for a profile (or all profiles)."""
    pm = _pm()
    profiles = [pm.get(name)] if name else pm.list()
    from claudex.history.browser import HistoryBrowser
    browser = HistoryBrowser(profiles)
    sessions = browser.get_all_sessions(profile_filter=name, limit=limit)
    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return
    table = Table(title="Sessions", header_style="bold cyan")
    table.add_column("Profile", style="bold")
    table.add_column("Last Active")
    table.add_column("Title")
    table.add_column("Msgs")
    table.add_column("Tokens")
    table.add_column("Session ID")
    for s in sessions:
        table.add_row(
            s.profile_name, s.age_human, s.title[:60],
            str(s.message_count), f"{s.total_tokens.total:,}", s.session_id[:16] + "...",
        )
    console.print(table)


@session_group.command("resume")
@click.argument("name", required=False)
@click.option("--session-id", "-s", default=None, help="Specific session ID to resume")
@click.option("--strategy", default="env", type=click.Choice(["env", "direct", "continue"]))
def session_resume(name: Optional[str], session_id: Optional[str], strategy: str) -> None:
    """Resume the last session for a profile."""
    try:
        pm = _pm()
        am = _auth()
        # Determine profile
        if not name:
            name = pm.get_active()
            if not name:
                console.print("[red]No active profile. Run 'claudex switch <name>' first.[/red]")
                sys.exit(1)
        profile = pm.get(name)
        from claudex.history.browser import HistoryBrowser
        browser = HistoryBrowser(pm.list())
        from claudex.core.session import SessionManager
        sm = SessionManager(am, browser)
        console.print(f"[cyan]Resuming session for profile [bold]{name}[/bold]...[/cyan]")
        sm.resume(name, profile.config_dir, session_id=session_id, strategy=strategy)
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@session_group.command("migrate")
@click.argument("session_id")
@click.option("--from", "from_profile", required=True, help="Source profile name")
@click.option("--to", "to_profile", required=True, help="Destination profile name")
def session_migrate(session_id: str, from_profile: str, to_profile: str) -> None:
    """Migrate a session from one profile to another."""
    pm = _pm()
    am = _auth()
    from claudex.history.browser import HistoryBrowser
    from claudex.history.parser import iter_sessions
    profiles = pm.list()
    browser = HistoryBrowser(profiles)
    # Find session
    session = next(
        (s for s in browser.get_all_sessions(profile_filter=from_profile)
         if s.session_id.startswith(session_id)),
        None,
    )
    if not session:
        console.print(f"[red]Session '{session_id}' not found in profile '{from_profile}'[/red]")
        sys.exit(1)
    to_prof = pm.get(to_profile)
    new_session = browser.migrate_session(session, to_profile, to_prof.config_dir)
    console.print(f"[green]✓[/green] Session migrated to profile '{to_profile}'")
    console.print(f"  New path: {new_session.file_path}")


# ─── History command ───────────────────────────────────────────────────────────

@cli.command("history")
@click.option("--profile", "-p", default=None)
@click.option("--limit", "-n", default=20)
def history_cmd(profile: Optional[str], limit: int) -> None:
    """Browse session history (opens TUI). Use --profile to filter."""
    from claudex.tui.app import run_app
    run_app()


@cli.command("search")
@click.argument("query")
@click.option("--profile", "-p", default=None)
def search_cmd(query: str, profile: Optional[str]) -> None:
    """Search session history."""
    pm = _pm()
    from claudex.history.browser import HistoryBrowser
    browser = HistoryBrowser(pm.list())
    results = browser.search(query, profile_filter=profile)
    if not results:
        console.print(f"[yellow]No sessions matching '{query}'[/yellow]")
        return
    table = Table(title=f"Search: {query}", header_style="bold cyan")
    table.add_column("Profile")
    table.add_column("Title")
    table.add_column("Last Active")
    table.add_column("Session ID")
    for s in results:
        table.add_row(s.profile_name, s.title[:60], s.age_human, s.session_id[:16] + "...")
    console.print(table)


# ─── Shell commands ────────────────────────────────────────────────────────────

@cli.group("shell")
def shell_group() -> None:
    """Shell integration management."""


@shell_group.command("setup")
@click.option("--shell", "-s", default="auto", type=click.Choice(["auto", "bash", "zsh", "powershell", "fish"]))
@click.option("--file", "-f", "init_file", default=None, help="Shell init file to install into")
@click.option("--print-only", is_flag=True, help="Print script without installing")
def shell_setup(shell: str, init_file: Optional[str], print_only: bool) -> None:
    """Install shell integration (aliases, switch functions, auto-hook)."""
    from claudex.shell import get_shell_integration
    pm = _pm()
    profiles = pm.list()
    integration = get_shell_integration(shell)
    script = integration.generate_init_script(profiles)
    if print_only:
        console.print(script)
        return
    dest = Path(init_file) if init_file else None
    installed_to = integration.install(profiles, dest)
    console.print(f"[green]✓[/green] Shell integration installed to {installed_to}")
    console.print("  Restart your shell or run:")
    console.print(f"    [cyan]source {installed_to}[/cyan]")


@shell_group.command("hook")
@click.option("--shell", "-s", default="auto")
def shell_hook(shell: str) -> None:
    """Print shell hook snippet (for manual inclusion)."""
    from claudex.shell import get_shell_integration
    pm = _pm()
    integration = get_shell_integration(shell)
    print(integration.generate_init_script(pm.list()))


# ─── Doctor ───────────────────────────────────────────────────────────────────

@cli.command("doctor")
def doctor_cmd() -> None:
    """Diagnose installation and configuration issues."""
    from claudex.commands.doctor import run_doctor
    run_doctor()


# ─── Internal (used by shell functions) ───────────────────────────────────────

@cli.group("_internal", hidden=True)
def internal_group() -> None:
    """Internal commands used by shell integration (not for direct use)."""


@internal_group.command("write-env")
@click.argument("profile_name")
def internal_write_env(profile_name: str) -> None:
    """Write env files for shell to source. Exit 0 on success, 1 on failure."""
    try:
        pm = _pm()
        pm.set_active(profile_name)
        sys.exit(0)
    except ClaudexError:
        sys.exit(1)


if __name__ == "__main__":
    cli()
