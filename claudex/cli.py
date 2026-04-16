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
  claudex auth refresh <name>           Refresh OAuth access token using refresh token
  claudex auth revoke <name>            Clear stored credentials

  -- Sessions --
  claudex session list                  List recent sessions across ALL profiles
  claudex session list [name]           Filter sessions by profile
  claudex session list --full-id        Show full session IDs (for copy-paste)
  claudex session resume [name]         Resume last session for a profile
  claudex session resume --from <name> -id <id>   Resume specific session by profile + ID
  claudex session resume -id <id>       Resume any session (auto-detects profile)
  claudex session migrate <id>          Move a session between profiles

  -- History --
  claudex history                       Open history browser (TUI)
  claudex search <query>                Search sessions by title/project

  -- Shell --
  claudex shell setup                   Install shell aliases + auto-switch hook
  claudex shell hook                    Print shell snippet (for manual inclusion)

  -- Profile Sharing --
  claudex share auth [--endpoint URL]           Log in to a sharing server
  claudex share push <profile> [--label TEXT]   Encrypt and upload profile; prints share token
  claudex share pull <token> <new-name> [--endpoint URL]  Download and decrypt a shared profile
  claudex share list [--endpoint URL]           List your shares on the server
  claudex share revoke <token-id> [--endpoint URL]  Revoke a share

  -- MCP --
  claudex mcp setup <profile> [--endpoint URL]  Register sharing MCP server in a profile

  -- Config --
  claudex config set sharing.endpoint <url>     Set default sharing endpoint
  claudex config get sharing.endpoint           Show current sharing endpoint

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


@auth_group.command("refresh")
@click.argument("name")
def auth_refresh(name: str) -> None:
    """Refresh the OAuth access token for a profile using its stored refresh token."""
    try:
        pm = _pm()
        am = _auth()
        profile = pm.get(name)
        console.print(f"[cyan]Refreshing token for profile [bold]{name}[/bold]...[/cyan]")
        status = am.refresh(name, profile.config_dir)
        console.print(f"[green]✓[/green] Token refreshed for '{name}'")
        console.print(f"  Expires: {status.expires_in_human}")
        if status.email:
            console.print(f"  Email:   {status.email}")
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
@click.option("--full-id", is_flag=True, help="Show full session IDs (for use with --resume)")
def session_list(name: Optional[str], limit: int, full_id: bool) -> None:
    """List sessions across all profiles (or filter by profile name)."""
    pm = _pm()
    profiles = [pm.get(name)] if name else pm.list()
    from claudex.history.browser import HistoryBrowser
    browser = HistoryBrowser(profiles)
    sessions = browser.get_all_sessions(profile_filter=name, limit=limit)
    if not sessions:
        target = f"profile '{name}'" if name else "any profile"
        console.print(f"[yellow]No sessions found in {target}.[/yellow]")
        return
    title = f"Sessions — {name}" if name else "Sessions — all profiles"
    table = Table(title=title, header_style="bold cyan")
    table.add_column("Profile", style="bold")
    table.add_column("Last Active")
    table.add_column("Title")
    table.add_column("Msgs")
    table.add_column("Tokens")
    table.add_column("Session ID", style="dim")
    for s in sessions:
        sid = s.session_id if full_id else s.session_id[:16] + "..."
        table.add_row(
            s.profile_name, s.age_human, s.title[:55],
            str(s.message_count), f"{s.total_tokens.total:,}", sid,
        )
    console.print(table)
    if not full_id:
        console.print("[dim]Tip: use --full-id to show complete session IDs for resuming[/dim]")
    console.print(f"[dim]Resume any session: claudex session resume --session-id <id>[/dim]")


@session_group.command("resume")
@click.argument("name", required=False)
@click.option("--from", "from_profile", default=None, help="Profile that owns the session (alias for the positional name)")
@click.option("--session-id", "-s", "-id", default=None, help="Session ID to resume (auto-detects profile if --from is omitted)")
@click.option("--strategy", default="direct", type=click.Choice(["env", "direct", "continue"]),
              help="Resume strategy: direct=--resume <id>, env=set CLAUDE_CONFIG_DIR only, continue=--continue")
def session_resume(name: Optional[str], from_profile: Optional[str], session_id: Optional[str], strategy: str) -> None:
    """Resume a session for a profile.

    \b
    Examples:
      claudex session resume                          # resume last session (active profile)
      claudex session resume work                     # resume last session for 'work'
      claudex session resume --from work -id <id>    # resume specific session from 'work'
      claudex session resume -id <id>                # auto-detect profile from session ID
    """
    try:
        pm = _pm()
        am = _auth()
        from claudex.history.browser import HistoryBrowser
        from claudex.core.session import SessionManager
        browser = HistoryBrowser(pm.list())
        sm = SessionManager(am, browser)

        # --from takes precedence over positional name
        name = from_profile or name

        # Cross-profile lookup: session-id given but no profile specified
        if session_id and not name:
            session = sm.find_by_id(session_id)
            if not session:
                console.print(f"[red]Session '{session_id}' not found in any profile.[/red]")
                console.print("  Use [cyan]claudex session list[/cyan] to browse sessions.")
                sys.exit(1)
            name = session.profile_name
            session_id = session.session_id  # Use full ID
            console.print(f"[dim]Found session in profile [bold]{name}[/bold][/dim]")

        # Fall back to active profile
        if not name:
            name = pm.get_active()
            if not name:
                console.print("[red]No active profile. Run 'claudex switch <name>' or pass a profile name.[/red]")
                sys.exit(1)

        profile = pm.get(name)

        # Auto-refresh expired OAuth token if a refresh token is available
        status = am.get_status(name, profile.config_dir)
        if status.is_expired and status.refresh_available:
            console.print(f"[yellow]Token expired — refreshing automatically...[/yellow]")
            try:
                am.refresh(name, profile.config_dir)
                console.print(f"[green]✓[/green] Token refreshed.")
            except ClaudexError as e:
                console.print(f"[yellow]Warning: token refresh failed: {e}[/yellow]")
                console.print("  Continuing anyway — Claude may prompt for re-auth.")

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


# ─── Config commands ──────────────────────────────────────────────────────────

@cli.group("config")
def config_group() -> None:
    """Manage claudex global configuration."""


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value (e.g. sharing.endpoint https://codder.cc)."""
    cfg = load_config()
    if key == "sharing.endpoint":
        cfg.sharing_endpoint = value
        cfg.save()
        console.print(f"[green]✓[/green] sharing.endpoint = {value}")
    else:
        console.print(f"[red]Unknown config key:[/red] {key}")
        sys.exit(1)


@config_group.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Get a configuration value."""
    cfg = load_config()
    if key == "sharing.endpoint":
        val = cfg.sharing_endpoint or "(not set)"
        console.print(f"sharing.endpoint = {val}")
    else:
        console.print(f"[red]Unknown config key:[/red] {key}")
        sys.exit(1)


# ─── Share commands ────────────────────────────────────────────────────────────

def _resolve_endpoint(endpoint_flag: Optional[str]) -> str:
    """Resolve sharing endpoint: flag > config > error."""
    if endpoint_flag:
        return endpoint_flag.rstrip("/")
    cfg = load_config()
    ep = cfg.sharing_endpoint
    if ep:
        return ep
    console.print(
        "[red]No sharing endpoint configured.[/red]\n"
        "  Run: [cyan]claudex config set sharing.endpoint <url>[/cyan]\n"
        "  Or pass [cyan]--endpoint <url>[/cyan] to this command."
    )
    sys.exit(1)


@cli.group("share")
def share_group() -> None:
    """Cross-machine encrypted profile sharing."""


@share_group.command("auth")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
def share_auth(endpoint: Optional[str]) -> None:
    """Authenticate to a profile sharing server and save credentials."""
    ep = _resolve_endpoint(endpoint)
    console.print(f"[cyan]Logging in to:[/cyan] {ep}")
    username = click.prompt("Username")
    password = click.prompt("Password", hide_input=True)

    from claudex.core.sharing_client import login, SharingAPIError
    try:
        login(ep, username, password)
        console.print(f"[green]✓[/green] Authenticated to {ep}")
    except SharingAPIError as e:
        console.print(f"[red]Login failed:[/red] {e.message}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@share_group.command("push")
@click.argument("profile_name")
@click.option("--label", "-l", default=None, help="Human-readable label for this share")
@click.option("--expires-days", default=None, type=int, help="Expiry in days (default: no expiry)")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
def share_push(profile_name: str, label: Optional[str], expires_days: Optional[int],
               endpoint: Optional[str]) -> None:
    """Encrypt and upload a profile config to the sharing server.

    Prints the share token — copy it to use on another machine with `claudex share pull`.
    Session history is NOT included. Credentials ARE included (encrypted).
    """
    ep = _resolve_endpoint(endpoint)
    pm = _pm()

    try:
        profile = pm.get(profile_name)
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if label is None:
        label = f"{profile_name} — {profile.email or 'unknown'}"

    console.print(f"[cyan]Bundling profile[/cyan] [bold]{profile_name}[/bold]...")

    from claudex.core.profile_bundle import export_bundle
    from claudex.crypto import generate_key, encrypt, encode_share_token
    from claudex.core.sharing_client import load_client, SharingAPIError
    from claudex.core.auth import AuthManager
    import base64

    # Flush latest tokens from OS Keychain → .credentials.json so the bundle
    # always contains a fresh accessToken + refreshToken.
    auth_mgr = AuthManager()
    flushed = auth_mgr.flush_credentials_to_file(profile_name, profile.config_dir)
    if flushed:
        console.print("[dim]Credentials flushed to bundle (accessToken + refreshToken)[/dim]")
    else:
        console.print("[yellow]Warning:[/yellow] No credentials found for this profile — bundle will have no auth tokens.")

    try:
        bundle_bytes = export_bundle(profile.config_dir)
    except Exception as e:
        console.print(f"[red]Failed to create bundle:[/red] {e}")
        sys.exit(1)

    aes_key = generate_key()
    ciphertext = encrypt(aes_key, bundle_bytes)
    ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")

    try:
        client = load_client(ep)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"[cyan]Uploading encrypted bundle to[/cyan] {ep}...")
    try:
        token_id = client.create_share(label, ciphertext_b64, expires_days)
    except SharingAPIError as e:
        console.print(f"[red]Upload failed:[/red] {e.message}")
        sys.exit(1)

    share_token = encode_share_token(token_id, aes_key)
    console.print()
    console.print(f"[green]✓[/green] Profile [bold]{profile_name}[/bold] shared successfully!")
    console.print()
    console.print(f"[bold]Share token:[/bold]")
    console.print(f"  [cyan]{share_token}[/cyan]")
    console.print()
    console.print("On another machine, run:")
    console.print(f"  [cyan]claudex share pull {share_token} <new-profile-name>[/cyan]")
    console.print()
    console.print("[dim]Keep this token secret — it contains the decryption key.[/dim]")


@share_group.command("pull")
@click.argument("share_token")
@click.argument("new_profile_name")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
def share_pull(share_token: str, new_profile_name: str, endpoint: Optional[str]) -> None:
    """Download and decrypt a shared profile.

    Creates a new profile called NEW_PROFILE_NAME with the downloaded config.
    """
    ep = _resolve_endpoint(endpoint)

    from claudex.crypto import decode_share_token, decrypt
    from claudex.core.profile_bundle import import_bundle
    from claudex.core.sharing_client import load_client, SharingAPIError
    import base64

    try:
        token_id, aes_key = decode_share_token(share_token)
    except ValueError as e:
        console.print(f"[red]Invalid share token:[/red] {e}")
        sys.exit(1)

    try:
        client = load_client(ep)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"[cyan]Fetching encrypted bundle from[/cyan] {ep}...")
    try:
        ciphertext_b64 = client.get_share(token_id)
    except SharingAPIError as e:
        console.print(f"[red]Download failed:[/red] {e.message}")
        sys.exit(1)

    try:
        ciphertext = base64.b64decode(ciphertext_b64)
        bundle_bytes = decrypt(aes_key, ciphertext)
    except ValueError as e:
        console.print(f"[red]Decryption failed:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error decrypting bundle:[/red] {e}")
        sys.exit(1)

    pm = _pm()
    try:
        existing = pm.get(new_profile_name)
        console.print(f"[yellow]Profile '{new_profile_name}' already exists — overwriting config files.[/yellow]")
        target_config_dir = existing.config_dir
    except ClaudexError:
        profile = pm.create(new_profile_name)
        target_config_dir = profile.config_dir

    console.print(f"[cyan]Extracting profile to[/cyan] {target_config_dir}...")
    try:
        import_bundle(bundle_bytes, target_config_dir)
    except Exception as e:
        console.print(f"[red]Failed to extract bundle:[/red] {e}")
        sys.exit(1)

    # Import credentials from the extracted .credentials.json into the local
    # credential backend (keyring) and OS Keychain (macOS) so Claude Code and
    # `claudex auth status` both work without a manual re-login.
    from claudex.core.auth import AuthManager
    auth_mgr = AuthManager()
    imported = auth_mgr.import_credentials_from_file(new_profile_name, target_config_dir)
    if imported:
        console.print("[dim]Credentials imported into local keyring (accessToken + refreshToken)[/dim]")
    else:
        console.print("[yellow]Note:[/yellow] No credentials found in bundle. Run `claudex auth add` to authenticate.")

    console.print()
    console.print(f"[green]✓[/green] Profile [bold]{new_profile_name}[/bold] restored!")
    console.print()
    console.print("Verify credentials:")
    console.print(f"  [cyan]claudex auth status {new_profile_name}[/cyan]")
    console.print()
    console.print("Launch with this profile:")
    console.print(f"  [cyan]claudex use {new_profile_name}[/cyan]")


@share_group.command("list")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
def share_list(endpoint: Optional[str]) -> None:
    """List your profile shares on the sharing server."""
    ep = _resolve_endpoint(endpoint)

    from claudex.core.sharing_client import load_client, SharingAPIError
    try:
        client = load_client(ep)
        shares = client.list_shares()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except SharingAPIError as e:
        console.print(f"[red]API error:[/red] {e.message}")
        sys.exit(1)

    if not shares:
        console.print("[yellow]No shares found.[/yellow]")
        return

    table = Table(title=f"Shares on {ep}", header_style="bold cyan")
    table.add_column("Token ID (partial)", style="dim")
    table.add_column("Label")
    table.add_column("Status")
    table.add_column("Accesses", justify="right")
    table.add_column("Created")
    table.add_column("Expires")

    for s in shares:
        token_id = s.get("token_id", "")
        label = s.get("label", "—")
        is_revoked = s.get("is_revoked", False)
        status = "[red]revoked[/red]" if is_revoked else "[green]active[/green]"
        accesses = str(s.get("access_count", 0))
        created = (s.get("created_at") or "—")[:10]
        expires = (s.get("expires_at") or "never")[:10]
        table.add_row(token_id[:18] + "...", label, status, accesses, created, expires)

    console.print(table)


@share_group.command("revoke")
@click.argument("token_id")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def share_revoke(token_id: str, endpoint: Optional[str], yes: bool) -> None:
    """Revoke a share by its server-side token ID."""
    ep = _resolve_endpoint(endpoint)

    if not yes:
        click.confirm(
            f"Revoke share '{token_id[:18]}...'? This cannot be undone.",
            abort=True,
        )

    from claudex.core.sharing_client import load_client, SharingAPIError
    try:
        client = load_client(ep)
        client.revoke_share(token_id)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except SharingAPIError as e:
        console.print(f"[red]Revoke failed:[/red] {e.message}")
        sys.exit(1)

    console.print(f"[green]✓[/green] Share revoked.")


# ─── MCP commands ─────────────────────────────────────────────────────────────

@cli.group("mcp")
def mcp_group() -> None:
    """MCP server management for Claude Code integration."""


@mcp_group.command("setup")
@click.argument("profile_name")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
@click.option("--name", "server_name", default="claudex-sharing",
              help="MCP server name in mcp_servers.json (default: claudex-sharing)")
def mcp_setup(profile_name: str, endpoint: Optional[str], server_name: str) -> None:
    """Register the claudex sharing MCP server in a profile's mcp_servers.json.

    After running this, start a Claude Code session with the profile and you can
    use the share_profile, pull_profile, list_profiles, and revoke_profile tools
    directly in a Claude conversation.
    """
    import json as _json

    ep = _resolve_endpoint(endpoint)
    pm = _pm()

    try:
        profile = pm.get(profile_name)
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # Load JWT from credential backend
    from claudex.core.sharing_client import SharingCredentials
    creds = SharingCredentials(ep)
    jwt = creds.load_jwt()
    if not jwt:
        console.print(
            f"[red]No credentials for {ep}.[/red]\n"
            "  Run: [cyan]claudex share auth --endpoint <url>[/cyan]"
        )
        sys.exit(1)

    mcp_path = profile.config_dir / "mcp_servers.json"
    if mcp_path.exists():
        try:
            mcp_config = _json.loads(mcp_path.read_text(encoding="utf-8"))
        except Exception:
            mcp_config = {}
    else:
        mcp_config = {}

    mcp_url = ep + "/api/v1/claudex/mcp"
    mcp_config[server_name] = {
        "type": "sse",
        "url": mcp_url,
        "headers": {
            "Authorization": f"Bearer {jwt}",
        },
    }
    mcp_path.write_text(_json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")

    console.print(f"[green]✓[/green] MCP server [bold]{server_name}[/bold] registered in profile [bold]{profile_name}[/bold]")
    console.print(f"  URL: {mcp_url}")
    console.print()
    console.print("Start a Claude Code session:")
    console.print(f"  [cyan]claudex use {profile_name}[/cyan]")
    console.print()
    console.print("Then use these MCP tools in your Claude conversation:")
    console.print("  [dim]share_profile[/dim], [dim]pull_profile[/dim], [dim]list_profiles[/dim], [dim]revoke_profile[/dim]")


if __name__ == "__main__":
    cli()
