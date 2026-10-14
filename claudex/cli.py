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
  claudex share pull <label> <new-name> [--endpoint URL]  Pull by label (no token needed if logged in)
  claudex share list [--endpoint URL]           List your shares on the server
  claudex share revoke <token-id> [--endpoint URL]  Revoke a share

  -- Fleet (multi-subscription agents) --
  claudex fleet dispatch "<prompt>" [-p name]   Run a headless `claude -p` job (auto-picks a profile)
  claudex fleet status [job_id]                 Show one job or a table of all
  claudex fleet result <job_id> [--raw]         Print a job's result
  claudex fleet logs <job_id> [--follow]        Tail a job's output log
  claudex fleet list [--status S] [-p name]     List jobs (advances the queue)
  claudex fleet cancel <job_id>                 Cancel a queued/running job
  claudex fleet tick [--max-starts N]           Advance the queue once (cron-friendly)
  claudex fleet fanout --task T --subtask S ... Fan one task out across profiles

  -- MCP --
  claudex mcp setup <profile> [--endpoint URL]  Register sharing MCP server in a profile
  claudex mcp setup <profile> --fleet           Register the local fleet MCP server (stdio)
  claudex mcp serve                             Run the local fleet MCP server (used by Claude)

  -- Config --
  claudex config set sharing.endpoint <url>     Set default sharing endpoint
  claudex config get sharing.endpoint           Show current sharing endpoint
  claudex config set autoupdate off|on          Pin claude's version (default: off)

  -- Other --
  claudex doctor                        Diagnose installation issues
  claudex update [--ref REF]            Pull latest source and reinstall via pipx
  claudex export <name>                 Export profile to .tar.gz
  claudex import <file>                 Import profile from .tar.gz
"""

from __future__ import annotations

import io
import json
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


def _seed_claude_json(config_dir: Path, seed_identity: bool = True) -> None:
    """Seed .claude.json in profile dir so interactive Claude skips the auth selector.

    Claude looks for .claude.json inside CLAUDE_CONFIG_DIR on startup to find the
    oauthAccount entry. Without it, interactive Claude shows the auth-type selection
    prompt even when .credentials.json is present (non-interactive -p works fine).
    We copy the essential fields from ~/.claude.json (home-level) to bootstrap it.

    seed_identity: when False, the account-identity keys (oauthAccount, userID) are
    NOT copied from the home profile. Use this after a fresh `auth add` /login,
    where the profile may belong to a DIFFERENT account than the home login —
    seeding the home account's oauthAccount there mismatches the just-stored token
    and forces a second authentication. Claude writes the correct oauthAccount
    itself during /login, so we must not overwrite it with the home account's.
    """
    import json as _json

    home_claude_json = Path.home() / ".claude.json"
    dest = config_dir / ".claude.json"

    # Fields that tell interactive Claude this profile is initialized
    SEED_KEYS = {
        "hasCompletedOnboarding", "lastOnboardingVersion",
        "installMethod", "autoUpdates",
    }
    if seed_identity:
        SEED_KEYS |= {"oauthAccount", "userID"}

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
        is_active = active == p.name or active == str(p.config_dir)
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


@cli.command("use", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def use_profile(name: str, claude_args: tuple) -> None:
    """Launch claude with a specific profile (one-shot, does not persist).

    Any extra arguments are passed straight through to `claude`, so profile
    flags and claude flags can be mixed freely:

    \b
      claudex use work --continue --dangerously-skip-permissions
      claudex use work -p "summarise this repo"
      claudex use work --model claude-opus-4-8

    If an argument would otherwise be captured by claudex, separate it with --:

    \b
      claudex use work -- --help      # shows claude's help, not claudex's
    """
    try:
        pm = _pm()
        am = _auth()
        profile = pm.get(name)
        env = {**os.environ, **am.get_env_for_profile(name, profile.config_dir)}
        from claudex.core.config import global_launch_env
        env.update(global_launch_env())
        from claudex.core.policy import augment_env, proxy_enabled
        augment_env(env, name, config_dir=profile.config_dir)
        cmd = [CLAUDE_BIN] + list(claude_args)

        if proxy_enabled(name):
            # Route through the shared proxy daemon (auto-started, reused across
            # every launch) so Claude Code sees a non-Anthropic base URL and
            # skips the org policy; telemetry off by default.
            from claudex.core.proxy import ensure_shared, proxy_env
            base = ensure_shared()
            if base:
                env.update(proxy_env(base))
                console.print(f"[dim]proxy on → {base} (org policy skipped, telemetry off)[/dim]")
            else:
                console.print("[yellow]warning:[/yellow] proxy mode requested but the "
                              "shared proxy could not start; launching without it.")

        if sys.platform != "win32":
            os.execvpe(cmd[0], cmd, env)  # replace this process (Unix)
        else:
            # No execvpe on Windows — run as a child and propagate its exit code.
            result = subprocess.run(cmd, env=env)
            sys.exit(result.returncode)
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
        from claudex.core.profile_bundle import safe_extract
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        # `export` writes arcnames relative to PROFILES_DIR (e.g. "work/..."), so
        # extract into PROFILES_DIR. safe_extract rejects traversal/symlink members.
        with tarfile.open(src, "r:gz") as tar:
            safe_extract(tar, PROFILES_DIR)
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
        # Don't seed the home account's identity: this profile may be a different
        # account, and Claude already wrote the correct oauthAccount during /login.
        _seed_claude_json(profile.config_dir, seed_identity=False)
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
@click.argument("profile_name", required=False, default=None)
def auth_status(profile_name: Optional[str]) -> None:
    """Show auth status for all profiles, or a specific one."""
    pm = _pm()
    am = _auth()
    try:
        profiles = [pm.get(profile_name)] if profile_name else pm.list()
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
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
    pm = _pm()
    if not pm.exists(name):
        console.print(f"[red]Error:[/red] Profile '{name}' not found. "
                      "Run 'claudex list' to see available profiles.")
        sys.exit(1)
    _auth().revoke(name)
    console.print(f"[green]✓[/green] Credentials cleared for '{name}'")


@auth_group.command("relogin")
@click.argument("name")
def auth_relogin(name: str) -> None:
    """Re-authenticate a profile by running `claude /login` with that profile's config dir.

    Use this when OAuth tokens have expired or become invalid (e.g. after pulling
    a profile from another machine).  Your profile settings, MCP servers, and
    CLAUDE.md are preserved — only the auth tokens are renewed.
    """
    try:
        pm = _pm()
        profile = pm.get(name)
        am = _auth()
        console.print(f"[cyan]Starting OAuth login for profile [bold]{name}[/bold]...[/cyan]")
        console.print("[dim]A browser window will open. Complete the login flow, then return here.[/dim]")
        am.add_account_oauth(name, profile.config_dir)
        console.print(f"[green]✓[/green] Re-authentication complete for profile '{name}'")
        console.print()
        console.print("Launch Claude with this profile:")
        console.print(f"  [cyan]claudex use {name}[/cyan]")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


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
    try:
        profiles = [pm.get(name)] if name else pm.list()
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
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
    from claudex.history.browser import HistoryBrowser
    try:
        profiles = pm.list()
        browser = HistoryBrowser(profiles)
        # Find session — prefer an exact id, then a unique prefix.
        candidates = [s for s in browser.get_all_sessions(profile_filter=from_profile)
                      if s.session_id == session_id or s.session_id.startswith(session_id)]
        exact = [s for s in candidates if s.session_id == session_id]
        candidates = exact or candidates
        if not candidates:
            console.print(f"[red]Session '{session_id}' not found in profile '{from_profile}'[/red]")
            sys.exit(1)
        if len(candidates) > 1:
            console.print(f"[red]Ambiguous session id '{session_id}'[/red] — matches "
                          f"{len(candidates)} sessions. Use a longer id (try --full-id).")
            sys.exit(1)
        to_prof = pm.get(to_profile)
        new_session = browser.migrate_session(candidates[0], to_profile, to_prof.config_dir)
    except (ClaudexError, FileExistsError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    console.print(f"[green]✓[/green] Session migrated to profile '{to_profile}'")
    console.print(f"  New path: {new_session.file_path}")


# ─── History command ───────────────────────────────────────────────────────────

@cli.command("history")
@click.option("--profile", "-p", default=None)
@click.option("--limit", "-n", default=20)
def history_cmd(profile: Optional[str], limit: int) -> None:
    """Browse session history.

    With no options this opens the interactive TUI. When --profile or --limit is
    given, a non-interactive table is printed instead (the TUI can't be
    pre-filtered from the CLI).
    """
    # No filters → interactive browser.
    if profile is None and limit == 20:
        from claudex.tui.app import run_app
        run_app()
        return
    # Filters given → render the same table as `session list` so the flags work.
    ctx = click.get_current_context()
    ctx.invoke(session_list, name=profile, limit=limit, full_id=False)


@cli.command("search")
@click.argument("query")
@click.option("--profile", "-p", default=None)
def search_cmd(query: str, profile: Optional[str]) -> None:
    """Search session history."""
    pm = _pm()
    from claudex.history.browser import HistoryBrowser
    try:
        profiles = [pm.get(profile)] if profile else pm.list()
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    browser = HistoryBrowser(profiles)
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


@cli.command("update")
@click.option("--ref", default=None,
              help="git ref to update to (branch/tag/commit). Defaults to current branch's upstream.")
def update_cmd(ref: Optional[str]) -> None:
    """Pull latest claudex source and reinstall via pipx."""
    from claudex.commands.update import run_update
    run_update(ref=ref)


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


@internal_group.command("switch-request")
def internal_switch_request() -> None:
    """UserPromptSubmit hook: turn a `/switch <target>` prompt into a switch
    request the supervisor applies. Reads the hook JSON on stdin.

    Never touches credentials. Writes only ~/.claudex/.switch_request, and only
    for a valid `/switch`. Any other prompt passes straight through (exit 0, no
    output), so the hook is invisible during normal use.
    """
    from claudex.core.supervisor import handle_switch_hook
    stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    out, code = handle_switch_hook(stdin_text)
    if out:
        sys.stdout.write(out)
    sys.exit(code)


@internal_group.command("hook-config")
def internal_hook_config() -> None:
    """Print the settings.json snippet that registers the /switch hook."""
    import json as _json
    snippet = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command",
                            "command": "claudex _internal switch-request"}]}
            ]
        }
    }
    print(_json.dumps(snippet, indent=2))


@internal_group.command("restore-hook")
@click.argument("name")
def internal_restore_hook(name: str) -> None:
    """Remove the /switch hook from a profile's settings.json (manual cleanup
    if a supervisor exited without restoring it)."""
    from claudex.core.supervisor import restore_hook
    try:
        pm = _pm()
        restore_hook(pm.get(name).config_dir)
        console.print(f"[green]✓[/green] Hook restored/removed for '{name}'.")
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ─── Supervisor (in-session switch) ───────────────────────────────────────────

@cli.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.option("--dry-run", is_flag=True,
              help="Wait for one /switch, print the launch plan, do NOT spawn/kill claude.")
@click.option("--timeout", type=float, default=None,
              help="Dry-run only: seconds to wait for a /switch before giving up.")
@click.option("--install-hook/--no-install-hook", default=True,
              help="Install the /switch hook into each profile's settings.json "
                   "(restored on exit). On by default.")
@click.option("--proxy/--no-proxy", "proxy", default=None,
              help="Force proxy mode on/off for this run, overriding the "
                   "profile's stored `policy proxy` setting.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def run_supervisor(name: str, dry_run: bool, timeout: Optional[float],
                   install_hook: bool, proxy: Optional[bool], claude_args: tuple) -> None:
    """Run claude under a supervisor that applies in-session profile switches.

    Live mode (default): hosts claude under NAME; when you type
    `switch <profile>` (a plain message, no slash) in the session, the
    supervisor carries the conversation into that profile and relaunches
    `claude --resume` there. Switching never touches credentials (Model 1:
    transcript carry only).

    `--dry-run` waits for a single switch, prints the relaunch plan, and exits
    without spawning or killing claude.
    """
    from claudex.core.supervisor import Supervisor
    try:
        if dry_run:
            sup = Supervisor(name, dry_run=True, extra_args=list(claude_args),
                             install_hook=False)
            console.print(f"[cyan]Supervisor (dry-run) active for [bold]{name}[/bold]. "
                          f"Waiting for a /switch request…[/cyan]")
            plan = sup.run_dry(timeout=timeout)
            if plan is None:
                console.print("[yellow]No /switch request received within the timeout.[/yellow]")
                sys.exit(1)
            console.print(f"[green]✓[/green] Switch request → [bold]{plan.target}[/bold]")
            console.print(f"  Plan: {plan.describe()}")
            console.print(f"  Command: [cyan]{' '.join(plan.cmd)}[/cyan]")
            return
        sup = Supervisor(name, extra_args=list(claude_args), install_hook=install_hook,
                         proxy_override=proxy)
        console.print(f"[cyan]Launching claude under profile [bold]{name}[/bold] "
                      f"(supervisor). Type [bold]switch <profile>[/bold] "
                      f"(a plain message, no slash) to switch.[/cyan]")
        rc = sup.run()
        sys.exit(rc)
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except FileNotFoundError:
        console.print("[red]Error:[/red] Claude CLI not found. Install from https://claude.ai/code")
        sys.exit(1)


# ─── Policy commands (local remote-settings override) ─────────────────────────

def _resolve_policy_profile(name: Optional[str]) -> str:
    """Resolve an optional profile arg to a concrete, existing profile name,
    defaulting to the active profile. Exits with a helpful error otherwise."""
    pm = _pm()
    if not name:
        name = pm.get_active()
    if not name or not pm.exists(name):
        console.print("[red]Error:[/red] no such profile. Pass a profile name "
                      "explicitly (see [cyan]claudex list[/cyan]).")
        sys.exit(1)
    return name


@cli.group("policy")
def policy_group() -> None:
    """Locally control Claude Code's remote managed settings, per profile.

    Claude Code pulls enterprise "managed settings" from the backend and applies
    them as a read-only, top-priority layer you can't soften locally. These
    commands intercept that one endpoint for a profile so you decide what it
    sees: reject it, freeze the current cache, or serve your own settings.
    """


@policy_group.command("show")
@click.argument("name", required=False, default=None)
def policy_show(name: Optional[str]) -> None:
    """Show the active policy for a profile (default: active profile)."""
    from claudex.core.policy import get_policy
    name = _resolve_policy_profile(name)
    pol = get_policy(name)
    mode = pol["mode"]
    desc = {
        "off": "off — Claude Code uses the real backend",
        "reject": "reject — endpoint answers 204, no managed settings applied",
        "freeze": "freeze — endpoint answers 304, keeps the cached file as-is",
        "set": "set — serving your own managed settings",
    }[mode]
    label = pol.get("label")
    suffix = f" [{label}]" if label else ""
    console.print(f"Profile [bold]{name}[/bold]: [cyan]{desc}[/cyan]{suffix}")
    if mode == "set":
        keys = sorted((pol.get("settings") or {}).keys())
        console.print(f"  settings keys: {', '.join(keys) if keys else '(empty object)'}")

    # Live state of the on-disk managed file (what Claude Code actually reads).
    from claudex.core.policy import (read_remote_file, _deny_count,
                                      REMOTE_SETTINGS_BACKUP, is_immutable,
                                      remote_settings_path)
    cfg = _pm().get(name).config_dir
    live = read_remote_file(cfg)
    rpath = cfg / "remote-settings.json"
    if live is None:
        console.print(f"  on-disk remote-settings.json: [dim]absent[/dim] ({rpath})")
    else:
        dc = _deny_count(live)
        deny_s = (f"[red]{dc} deny rule(s)[/red]" if dc else "[green]no deny rules[/green]")
        locked = " [yellow]🔒 immutable[/yellow]" if is_immutable(remote_settings_path(cfg)) else ""
        console.print(f"  on-disk remote-settings.json: keys={sorted(live.keys()) or '∅'}, {deny_s}{locked}")
    if pol.get("proxy"):
        console.print("  [green]proxy mode ON[/green] — launches route through a local "
                      "api.anthropic.com proxy (org policy skipped, fail-open orgs)")
    if pol.get("lock"):
        console.print("  [dim]lock requested — file made immutable at each launch[/dim]")
    if (cfg / REMOTE_SETTINGS_BACKUP).exists():
        console.print(f"  [dim]org backup kept → `claudex policy off {name}` restores it[/dim]")


@policy_group.command("log")
@click.argument("name", required=False, default=None)
@click.option("--lines", "-n", default=20, show_default=True,
              help="How many recent entries to show.")
@click.option("--clear", is_flag=True, help="Clear the log instead of showing it.")
def policy_log(name: Optional[str], lines: int, clear: bool) -> None:
    """Show the shim's activity report for a profile (launches + interceptions)."""
    from claudex.core.policy import read_log, clear_log, log_path
    name = _resolve_policy_profile(name)
    if clear:
        clear_log(name)
        console.print(f"[green]✓[/green] Cleared policy log for [bold]{name}[/bold].")
        return
    records = read_log(name, lines=lines)
    if not records:
        console.print(f"No policy activity logged for [bold]{name}[/bold] yet.")
        console.print(f"[dim]Log file: {log_path(name)}[/dim]")
        console.print("[dim]Launch the profile with an active policy, then re-run this.[/dim]")
        return
    console.print(f"[bold]Policy activity for {name}[/bold] (last {len(records)}):")
    for r in records:
        ts = str(r.get("ts", ""))[:19].replace("T", " ")
        ev = r.get("event")
        if ev == "launch":
            inj = r.get("injected_env") or {}
            inj_s = ", ".join(f"{k}={v}" for k, v in inj.items()) or "none"
            rf = r.get("remote_file")
            if isinstance(rf, dict) and rf.get("wrote"):
                rd = rf.get("removed_deny") or 0
                keys = ", ".join(rf.get("keys") or []) or "∅"
                rf_s = f"wrote remote-settings.json keys=[{keys}]" + (
                    f", stripped {rd} deny rule(s)" if rd else "")
            else:
                rf_s = "no file write"
            console.print(f"  [dim]{ts}[/dim] [cyan]launch[/cyan] "
                          f"mode={r.get('mode')} label={r.get('label') or '-'} "
                          f"privacy_env=[{inj_s}]")
            if r.get("override_file"):
                console.print("           [dim]↳ CLAUDE_CODE_REMOTE_SETTINGS_PATH set "
                              "→ API fetch skipped[/dim]")
            console.print(f"           [dim]↳ {rf_s}[/dim]")
        elif ev == "settings_intercept":
            keys = ", ".join(r.get("served_keys") or []) or "∅"
            console.print(f"  [dim]{ts}[/dim] [green]intercept[/green] "
                          f"GET settings → [bold]{r.get('status')}[/bold] "
                          f"(mode={r.get('mode')}, served=[{keys}])")
        else:
            console.print(f"  [dim]{ts}[/dim] {ev} {r}")


@policy_group.command("templates")
def policy_templates() -> None:
    """List available policy templates (built-in + your own)."""
    from claudex.core.policy import list_templates
    templates = list_templates()
    if not templates:
        console.print("No templates available.")
        return
    console.print("[bold]Policy templates:[/bold]")
    for tname, src in templates.items():
        tag = "built-in" if src == "built-in" else "custom"
        console.print(f"  [cyan]{tname}[/cyan] ([dim]{tag}[/dim])")
    console.print("\nApply with [cyan]claudex policy default[/cyan] or "
                  "[cyan]claudex policy set <profile> -t <name>[/cyan].")
    console.print(f"Add your own: drop [cyan]<name>.json[/cyan] into "
                  f"[dim]{CLAUDEX_HOME / 'policy' / 'templates'}[/dim].")


@policy_group.command("default")
@click.argument("name", required=False, default=None)
@click.option("--template", "-t", "template_name", default="default", show_default=True,
              help="Which template to apply (see `claudex policy templates`).")
@click.option("--lock/--no-lock", "-L", default=False,
              help="Make remote-settings.json immutable so a background re-sync "
                   "can't restore the org policy (macOS/Linux).")
def policy_default(name: Optional[str], template_name: str, lock: bool) -> None:
    """Replace remote with a ready-made template (default: permissive, no telemetry)."""
    from claudex.core.policy import apply_template
    name = _resolve_policy_profile(name)
    try:
        apply_template(name, template_name, lock=lock)
    except (KeyError, ValueError, OSError) as e:
        console.print(f"[red]Error:[/red] template '{template_name}' not usable: {e}")
        sys.exit(1)
    lk = " [dim](locked)[/dim]" if lock else ""
    console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] will apply the "
                  f"[cyan]{template_name}[/cyan] template instead of the remote policy.{lk}")


@policy_group.command("reject")
@click.argument("name", required=False, default=None)
def policy_reject(name: Optional[str]) -> None:
    """Reject remote managed settings for a profile (serve 204)."""
    from claudex.core.policy import set_policy
    name = _resolve_policy_profile(name)
    set_policy(name, "reject")
    console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] will reject remote "
                  f"managed settings (next launch / within the hour).")


@policy_group.command("freeze")
@click.argument("name", required=False, default=None)
def policy_freeze(name: Optional[str]) -> None:
    """Freeze the current cached remote settings for a profile (serve 304)."""
    from claudex.core.policy import set_policy
    name = _resolve_policy_profile(name)
    set_policy(name, "freeze")
    console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] will keep its cached "
                  f"remote-settings.json (no refetch overwrite).")


@policy_group.command("set")
@click.argument("name", required=False, default=None)
@click.option("--file", "-f", "settings_file", default=None,
              type=click.Path(exists=True, dir_okay=False),
              help="JSON file with the managed settings object to serve.")
@click.option("--template", "-t", "template_name", default=None,
              help="Use a named template instead of a file "
                   "(see `claudex policy templates`).")
@click.option("--lock/--no-lock", "-L", default=False,
              help="Make remote-settings.json immutable so a background re-sync "
                   "can't restore the org policy (macOS/Linux).")
def policy_set(name: Optional[str], settings_file: Optional[str],
               template_name: Optional[str], lock: bool) -> None:
    """Serve your own managed settings for a profile (serve 200)."""
    from claudex.core.policy import set_policy, apply_template
    name = _resolve_policy_profile(name)
    if bool(settings_file) == bool(template_name):
        console.print("[red]Error:[/red] pass exactly one of --file or --template.")
        sys.exit(1)

    if template_name:
        try:
            pol = apply_template(name, template_name, lock=lock)
        except (KeyError, ValueError, OSError) as e:
            console.print(f"[red]Error:[/red] template '{template_name}' not usable: {e}")
            sys.exit(1)
        keys = sorted((pol.get("settings") or {}).keys())
        console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] will apply template "
                      f"[cyan]{template_name}[/cyan] ({', '.join(keys) or '∅'}).")
        return

    try:
        settings = json.loads(Path(settings_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        console.print(f"[red]Error:[/red] could not read JSON from {settings_file}: {e}")
        sys.exit(1)
    if not isinstance(settings, dict):
        console.print("[red]Error:[/red] settings file must contain a JSON object.")
        sys.exit(1)
    set_policy(name, "set", settings, lock=lock)
    keys = sorted(settings.keys())
    console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] will apply your managed "
                  f"settings ({len(keys)} key(s): {', '.join(keys) or '∅'}).")


@policy_group.command("proxy")
@click.argument("name", required=False, default=None)
@click.option("--off", is_flag=True, help="Disable proxy mode for the profile.")
def policy_proxy(name: Optional[str], off: bool) -> None:
    """Route a profile through the local api.anthropic.com proxy.

    Claude Code then sees a non-Anthropic base URL and skips the org managed
    policy (works only for fail-open orgs), while inference still reaches the
    real API. `run`/`use` start and stop the proxy automatically.
    """
    from claudex.core.policy import set_proxy
    name = _resolve_policy_profile(name)
    set_proxy(name, not off)
    if off:
        console.print(f"[green]✓[/green] Proxy mode OFF for [bold]{name}[/bold].")
    else:
        console.print(f"[green]✓[/green] Proxy mode ON for [bold]{name}[/bold] — "
                      f"[cyan]claudex run {name}[/cyan] (or [cyan]use[/cyan]) will skip the "
                      f"org managed policy via a local proxy.")


@policy_group.command("off")
@click.argument("name", required=False, default=None)
def policy_off(name: Optional[str]) -> None:
    """Disable the local override; Claude Code uses the real backend again."""
    from claudex.core.policy import set_policy, restore_remote_file
    name = _resolve_policy_profile(name)
    set_policy(name, "off")
    restored = restore_remote_file(_pm().get(name).config_dir)
    note = " (restored the org-pushed remote-settings.json)" if restored else ""
    console.print(f"[green]✓[/green] Profile [bold]{name}[/bold] back to the real "
                  f"backend for managed settings.{note}")


@cli.command("proxy")
@click.option("--start", "action", flag_value="start", help="Start the shared proxy daemon.")
@click.option("--stop", "action", flag_value="stop", help="Stop the shared proxy daemon.")
@click.option("--status", "action", flag_value="status", help="Show the shared proxy status.")
@click.option("--daemon", "action", flag_value="daemon", hidden=True,
              help="(internal) run the daemon body in the foreground.")
@click.option("--port", "-p", default=0, show_default="auto (free port)",
              help="Local port to listen on (0 = pick a free one).")
@click.option("--host", default="127.0.0.1", show_default=True, help="Local bind address.")
def proxy_cmd(action: Optional[str], port: int, host: str) -> None:
    """Manage the local reverse proxy to api.anthropic.com.

    A single shared daemon serves every proxy-mode launch (use/run/fleet), so
    even 100 concurrent `claude -p` share one forwarder. It auto-starts on first
    use; these flags let you manage it explicitly.

    \b
      claudex proxy --start     # start the shared daemon (prints its URL)
      claudex proxy --status    # is it running? on what URL?
      claudex proxy --stop      # stop it

    With no flag, runs a one-off proxy in the foreground (Ctrl+C to stop). Only
    works where the org policy is fail-open.
    """
    from claudex.core import proxy as pxy

    if action == "daemon":
        pxy.run_daemon(host, port)
        return
    if action == "status":
        st = pxy.status()
        if st["running"]:
            console.print(f"[green]running[/green] → [bold]{st['url']}[/bold] (pid {st['pid']})")
        else:
            console.print("[yellow]not running[/yellow]")
        return
    if action == "stop":
        stopped = pxy.stop_shared()
        console.print("[green]✓[/green] stopped." if stopped else "[dim]nothing to stop.[/dim]")
        return
    if action == "start":
        url = pxy.ensure_shared(host)
        if url:
            console.print(f"[green]✓[/green] shared proxy at [bold]{url}[/bold] "
                          f"→ api.anthropic.com")
        else:
            console.print("[red]Error:[/red] could not start the shared proxy daemon.")
            sys.exit(1)
        return

    # No flag: one-off foreground proxy.
    try:
        server = pxy.make_server(host, port)
    except OSError as e:
        console.print(f"[red]Error:[/red] could not bind {host}:{port}: {e}")
        sys.exit(1)
    base = pxy.base_url_for(server)
    console.print(f"[cyan]claudex proxy[/cyan] → forwarding [bold]{base}[/bold] "
                  f"to [bold]api.anthropic.com[/bold]  [dim](Ctrl+C to stop)[/dim]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


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
    elif key == "autoupdate":
        on = value.strip().lower() in ("on", "true", "1", "yes", "enabled")
        cfg.disable_autoupdate = not on
        cfg.save()
        state = "ON (claude will auto-update)" if on else "OFF (DISABLE_AUTOUPDATER=1 on every launch)"
        console.print(f"[green]✓[/green] autoupdate = {state}")
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
    elif key == "autoupdate":
        console.print(f"autoupdate = {'off' if cfg.disable_autoupdate else 'on'}")
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

    # Generate key + encrypt bundle, build cx_ token — all client-side before upload.
    # NOTE: the cx_ token (which embeds the AES key) is uploaded below so that
    # `share pull <label>` works without copy-pasting the token. This is a
    # convenience trade-off, not zero-knowledge — see claudex/crypto.py.
    aes_key = generate_key()
    ciphertext = encrypt(aes_key, bundle_bytes)
    ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
    share_token = encode_share_token(aes_key)  # builds token with a fresh UUID inside

    try:
        client = load_client(ep)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"[cyan]Uploading encrypted bundle to[/cyan] {ep}...")
    try:
        token_id = client.create_share(label, ciphertext_b64, share_token, expires_days)
    except SharingAPIError as e:
        console.print(f"[red]Upload failed:[/red] {e.message}")
        sys.exit(1)
    console.print()
    console.print(f"[green]✓[/green] Profile [bold]{profile_name}[/bold] shared as [bold]\"{label}\"[/bold]")
    console.print()
    console.print("On another machine, run:")
    console.print(f"[cyan]claudex share pull \"{label}\" PROFILE_NAME[/cyan]")
    console.print()
    console.print("[dim]That's it — no token needed if you're logged in to the same account.[/dim]")


@share_group.command("pull")
@click.argument("label_or_token")
@click.argument("new_profile_name")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
def share_pull(label_or_token: str, new_profile_name: str, endpoint: Optional[str]) -> None:
    """Download and decrypt a shared profile.

    LABEL_OR_TOKEN can be the share label (e.g. "work") or a cx_ token.
    If you're logged in to the same account on this machine, just use the label.

    Creates a new profile called NEW_PROFILE_NAME with the downloaded config.
    """
    ep = _resolve_endpoint(endpoint)

    from claudex.crypto import decode_share_token, decrypt
    from claudex.core.profile_bundle import import_bundle
    from claudex.core.sharing_client import load_client, SharingAPIError
    import base64

    label_or_token = label_or_token.strip()
    is_token = label_or_token.startswith("cx_")

    try:
        client = load_client(ep)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"[cyan]Fetching encrypted bundle from[/cyan] {ep}...")

    if is_token:
        # Explicit cx_ token provided — decode key from it, fetch by token_id
        try:
            token_id, aes_key = decode_share_token(label_or_token)
        except ValueError as e:
            console.print(f"[red]Invalid share token:[/red] {e}")
            sys.exit(1)
        try:
            ciphertext_b64 = client.get_share(token_id)
        except SharingAPIError as e:
            console.print(f"[red]Download failed:[/red] {e.message}")
            sys.exit(1)
    else:
        # Pull by label — server returns both ciphertext and stored cx_ token
        try:
            resp = client.get_share_by_label(label_or_token)
        except SharingAPIError as e:
            console.print(f"[red]Download failed:[/red] {e.message}")
            sys.exit(1)
        stored_token = resp.get("share_token", "")
        if not stored_token:
            console.print("[red]Server has no token stored for this share.[/red]")
            console.print("[dim]Re-push the profile with a newer claudex to enable pull-by-label.[/dim]")
            sys.exit(1)
        try:
            _, aes_key = decode_share_token(stored_token)
        except ValueError as e:
            console.print(f"[red]Stored token is invalid:[/red] {e}")
            sys.exit(1)
        ciphertext_b64 = resp["encrypted_data"]

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

    # Fix profile.toml: the bundle contains the source profile's name — overwrite
    # it with the requested new name so `claudex list` / `auth status` show correctly.
    profile_toml = target_config_dir / "profile.toml"
    if profile_toml.exists():
        try:
            try:
                import tomllib as _tomllib
            except ImportError:
                import tomli as _tomllib  # type: ignore
            import tomli_w as _tomli_w  # type: ignore[import]
            data = _tomllib.loads(profile_toml.read_text(encoding="utf-8"))
            data["name"] = new_profile_name
            profile_toml.write_text(_tomli_w.dumps(data), encoding="utf-8")
        except Exception:
            # tomli_w may not be installed; fall back to simple line replace
            text = profile_toml.read_text(encoding="utf-8")
            import re as _re
            text = _re.sub(r'^name\s*=\s*"[^"]*"', f'name = "{new_profile_name}"', text, flags=_re.MULTILINE)
            profile_toml.write_text(text, encoding="utf-8")

    # Import credentials from .credentials.json into local keyring + macOS Keychain.
    from claudex.core.auth import AuthManager
    auth_mgr = AuthManager()
    imported = auth_mgr.import_credentials_from_file(new_profile_name, target_config_dir)
    creds_ok = False
    if imported:
        console.print("[dim]Credentials found in bundle — importing into local keyring...[/dim]")
        # Try to refresh immediately — always attempt refresh after pull because the source
        # machine may have already refreshed its session, making the bundled access token
        # server-side invalid even if the client-side expiresAt hasn't passed yet.
        try:
            console.print("[cyan]Verifying OAuth tokens (refreshing session)...[/cyan]")
            auth_mgr.refresh(new_profile_name, target_config_dir)
            console.print("[green]✓[/green] [dim]Credentials verified and refreshed.[/dim]")
            creds_ok = True
        except Exception as refresh_err:
            console.print(f"[yellow]Warning:[/yellow] Could not refresh OAuth token: {refresh_err}")
            console.print("[yellow]The bundled tokens may have been invalidated on the source machine.[/yellow]")
        # On macOS, Claude Code reads the Keychain, not .credentials.json. Verify the
        # token actually landed there — otherwise claudex shows ✓ while Claude 401s.
        kc = auth_mgr.macos_keychain_token_present(target_config_dir)
        if kc is False:
            creds_ok = False
            console.print(
                "[yellow]Warning:[/yellow] could not write the macOS Keychain entry "
                "Claude Code reads (locked keychain or naming drift)."
            )
    else:
        console.print(f"[yellow]No credentials in bundle.[/yellow]")

    console.print()
    console.print(f"[green]✓[/green] Profile [bold]{new_profile_name}[/bold] restored!")
    console.print()
    if creds_ok:
        console.print("Launch with this profile:")
        console.print(f"  [cyan]claudex use {new_profile_name}[/cyan]")
    else:
        console.print("[bold yellow]Action required — re-authenticate:[/bold yellow]")
        console.print(f"  [cyan]claudex auth relogin {new_profile_name}[/cyan]")
        console.print()
        console.print("[dim]  (This runs `claude /login` with your profile — takes ~30 seconds.[/dim]")
        console.print("[dim]   Your profile config, MCP servers, and settings are all intact.)[/dim]")


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


def _register_mcp_server(profile, server_name: str, entry: dict) -> None:
    """Merge an MCP server entry into a profile's mcp_servers.json."""
    import json as _json

    mcp_path = profile.config_dir / "mcp_servers.json"
    if mcp_path.exists():
        try:
            mcp_config = _json.loads(mcp_path.read_text(encoding="utf-8"))
        except Exception:
            mcp_config = {}
    else:
        mcp_config = {}
    mcp_config[server_name] = entry
    mcp_path.write_text(_json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")


@mcp_group.command("serve")
@click.option("--transport", default="stdio", type=click.Choice(["stdio"]),
              help="MCP transport (only stdio supported)")
def mcp_serve(transport: str) -> None:
    """Run the local fleet MCP server (blocking). Launched by Claude over stdio."""
    try:
        from claudex.fleet.mcp_server import serve_stdio
    except ImportError:
        console.print(
            r"[red]The fleet MCP server needs the 'mcp' SDK.[/red]" "\n"
            r"  Install with: [cyan]pipx install 'claudex\[fleet]'[/cyan] "
            r"or [cyan]pip install 'claudex\[fleet]'[/cyan]"
        )
        sys.exit(1)
    serve_stdio()


@mcp_group.command("setup")
@click.argument("profile_name")
@click.option("--endpoint", "-e", default=None, help="Sharing server URL (overrides config)")
@click.option("--fleet", "fleet", is_flag=True,
              help="Register the local fleet MCP server (stdio) instead of the sharing server")
@click.option("--name", "server_name", default=None,
              help="MCP server name in mcp_servers.json")
def mcp_setup(profile_name: str, endpoint: Optional[str], fleet: bool,
              server_name: Optional[str]) -> None:
    """Register a claudex MCP server in a profile's mcp_servers.json.

    Default registers the sharing MCP server (share_profile, pull_profile, ...).
    With --fleet, registers the local stdio fleet server so Claude can dispatch
    agent jobs across your other subscriptions (fleet_dispatch, fleet_status, ...).
    """
    import json as _json

    pm = _pm()
    try:
        profile = pm.get(profile_name)
    except ClaudexError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if fleet:
        name = server_name or "claudex-fleet"
        claudex_bin = shutil.which("claudex")
        if claudex_bin:
            command, args = claudex_bin, ["mcp", "serve"]
        else:
            command, args = sys.executable, ["-m", "claudex.cli", "mcp", "serve"]
        _register_mcp_server(profile, name, {
            "type": "stdio",
            "command": command,
            "args": args,
        })
        console.print(f"[green]✓[/green] Fleet MCP server [bold]{name}[/bold] registered in profile [bold]{profile_name}[/bold]")
        console.print()
        console.print("Start a Claude Code session:")
        console.print(f"  [cyan]claudex use {profile_name}[/cyan]")
        console.print()
        console.print("Then use these MCP tools in your conversation:")
        console.print("  [dim]fleet_dispatch[/dim], [dim]fleet_status[/dim], [dim]fleet_result[/dim], "
                      "[dim]fleet_fanout[/dim], [dim]fleet_list_profiles[/dim], [dim]fleet_cancel[/dim]")
        return

    server_name = server_name or "claudex-sharing"
    ep = _resolve_endpoint(endpoint)

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
    # This file now holds a bearer token — restrict it to the owner.
    try:
        mcp_path.chmod(0o600)
    except OSError:
        pass

    console.print(f"[green]✓[/green] MCP server [bold]{server_name}[/bold] registered in profile [bold]{profile_name}[/bold]")
    console.print(f"  URL: {mcp_url}")
    console.print()
    console.print("Start a Claude Code session:")
    console.print(f"  [cyan]claudex use {profile_name}[/cyan]")
    console.print()
    console.print("Then use these MCP tools in your Claude conversation:")
    console.print("  [dim]share_profile[/dim], [dim]pull_profile[/dim], [dim]list_profiles[/dim], [dim]revoke_profile[/dim]")


# ─── Fleet commands ───────────────────────────────────────────────────────────

@cli.group("fleet")
def fleet_group() -> None:
    """Run headless `claude -p` agent jobs across multiple profiles (subscriptions)."""


def _engine():
    from claudex.fleet.engine import FleetEngine
    return FleetEngine()


def _status_style(status: str) -> str:
    return {
        "succeeded": "green",
        "running": "cyan",
        "assigned": "cyan",
        "queued": "yellow",
        "rate_limited": "yellow",
        "failed": "red",
        "cancelled": "dim",
    }.get(status, "white")


def _parse_status(status_filter):
    """Validate a --status value, exiting with a friendly error on a bad one."""
    from claudex.fleet.models import JobStatus
    if not status_filter:
        return None
    try:
        return JobStatus(status_filter)
    except ValueError:
        valid = ", ".join(s.value for s in JobStatus)
        console.print(f"[red]Error:[/red] invalid status '{status_filter}'. Valid: {valid}")
        sys.exit(1)


@fleet_group.command("dispatch")
@click.argument("prompt")
@click.option("--profile", "-p", default=None, help="Pin to a profile (default: auto-select)")
@click.option("--cwd", default=None, help="Working directory for the agent")
@click.option("--model", "-m", default=None, help="Model to use (passed to claude --model)")
@click.option("--timeout", "timeout_s", type=int, default=None, help="Per-job timeout in seconds")
@click.option("--max-attempts", type=int, default=3, help="Max attempts before giving up")
@click.option("--wait", is_flag=True, help="Block and poll until the job reaches a terminal state")
def fleet_dispatch(prompt, profile, cwd, model, timeout_s, max_attempts, wait) -> None:
    """Dispatch a headless agent job. Prints the job id."""
    eng = _engine()
    job = eng.dispatch(
        prompt, profile=profile, cwd=cwd, model=model,
        timeout_s=timeout_s, max_attempts=max_attempts,
    )
    console.print(f"[green]✓[/green] Job [bold]{job.id}[/bold] "
                  f"[{_status_style(job.status.value)}]{job.status.value}[/] "
                  f"→ profile [bold]{job.profile or '—'}[/bold]")
    if wait:
        _wait_for_job(eng, job.id)


def _wait_for_job(eng, job_id: str, max_wait_s: int = 1800) -> None:
    import time
    from claudex.fleet.models import JobStatus
    deadline = time.monotonic() + max_wait_s
    with console.status(f"Running {job_id}…"):
        while True:
            job = eng.status(job_id)
            if job.is_terminal():
                break
            if time.monotonic() > deadline:
                console.print(
                    f"[yellow]Timed out waiting for {job_id} "
                    f"(status: {job.status.value}).[/yellow]"
                )
                if job.status == JobStatus.QUEUED:
                    console.print("[dim]Job is still queued — no eligible profile? "
                                  "Check: claudex fleet status[/dim]")
                return
            time.sleep(2)
    job = eng.status(job_id, tick_first=False)
    console.print(f"Job [bold]{job.id}[/bold]: "
                  f"[{_status_style(job.status.value)}]{job.status.value}[/]")
    res = eng.result(job_id)
    if res and res.result_text:
        console.print(res.result_text)
    elif res and res.error:
        console.print(f"[red]{res.error}[/red]")


@fleet_group.command("status")
@click.argument("job_id", required=False)
def fleet_status(job_id) -> None:
    """Show one job, or a table of all jobs."""
    eng = _engine()
    if job_id:
        job = eng.status(job_id)
        console.print(f"[bold]{job.id}[/bold]  "
                      f"[{_status_style(job.status.value)}]{job.status.value}[/]")
        console.print(f"  profile : {job.profile or '—'}")
        console.print(f"  attempts: {job.attempts}/{job.max_attempts}")
        if job.failure_kind:
            console.print(f"  failure : {job.failure_kind}")
        if job.parent_id:
            console.print(f"  parent  : {job.parent_id}")
        if job.child_ids:
            console.print(f"  children: {', '.join(job.child_ids)}")
        console.print(f"  prompt  : {job.prompt[:120]}")
        return
    _print_job_table(eng.list_jobs())


def _print_job_table(jobs) -> None:
    if not jobs:
        console.print("[yellow]No fleet jobs.[/yellow] Dispatch one: claudex fleet dispatch \"<prompt>\"")
        return
    import humanize
    from datetime import datetime, timezone
    table = Table(title="Fleet jobs", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="bold")
    table.add_column("Status")
    table.add_column("Profile")
    table.add_column("Try")
    table.add_column("Age")
    table.add_column("Prompt")
    for j in jobs:
        try:
            age = humanize.naturaltime(datetime.now(timezone.utc) - j.created_at)
        except Exception:
            age = "—"
        kind = "orchestrator" if j.is_orchestrator else (j.profile or "—")
        table.add_row(
            j.id,
            f"[{_status_style(j.status.value)}]{j.status.value}[/]",
            kind,
            f"{j.attempts}/{j.max_attempts}",
            age,
            (j.prompt[:50] + "…") if len(j.prompt) > 50 else j.prompt,
        )
    console.print(table)


@fleet_group.command("list")
@click.option("--status", "status_filter", default=None, help="Filter by status")
@click.option("--profile", "-p", default=None, help="Filter by profile")
def fleet_list(status_filter, profile) -> None:
    """List fleet jobs (advances the queue first)."""
    st = _parse_status(status_filter)
    eng = _engine()
    eng.tick()
    _print_job_table(eng.list_jobs(status=st, profile=profile))


@fleet_group.command("result")
@click.argument("job_id")
@click.option("--raw", is_flag=True, help="Print the raw result JSON")
def fleet_result(job_id, raw) -> None:
    """Print a job's result."""
    eng = _engine()
    res = eng.result(job_id)
    if res is None:
        console.print("[yellow]No result yet.[/yellow] Check: claudex fleet status " + job_id)
        return
    if raw:
        import json as _json
        console.print_json(_json.dumps(res.to_dict()))
        return
    if res.result_text:
        console.print(res.result_text)
    if res.error:
        console.print(f"[red]{res.error}[/red]")
    if res.cost_usd is not None:
        console.print(f"[dim]cost ${res.cost_usd:.4f} · {res.num_turns} turns[/dim]")


@fleet_group.command("logs")
@click.argument("job_id")
@click.option("--follow", "-f", is_flag=True, help="Follow the log (tail -f)")
def fleet_logs(job_id, follow) -> None:
    """Print (or follow) a job's combined stdout/stderr log."""
    eng = _engine()
    path = eng.logs_path(job_id)
    if not path.exists():
        console.print("[yellow]No log yet.[/yellow]")
        return
    if not follow:
        console.print(path.read_text(encoding="utf-8", errors="replace"))
        return
    import time
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            sys.stdout.write(f.read())
            sys.stdout.flush()
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass


@fleet_group.command("cancel")
@click.argument("job_id")
def fleet_cancel(job_id) -> None:
    """Cancel a queued or running job."""
    eng = _engine()
    job = eng.cancel(job_id)
    console.print(f"[green]✓[/green] Job [bold]{job.id}[/bold] "
                  f"[{_status_style(job.status.value)}]{job.status.value}[/]")


@fleet_group.command("tick")
@click.option("--max-starts", type=int, default=None, help="Cap workers started this tick")
def fleet_tick(max_starts) -> None:
    """Advance the queue once (reconcile, retry, schedule). Cron-friendly."""
    eng = _engine()
    r = eng.tick(max_starts=max_starts)
    console.print(f"reconciled={r.reconciled} retried={r.retried} "
                  f"started={r.started} no-profile={r.skipped_no_profile}")


@fleet_group.command("fanout")
@click.option("--task", required=True, help="Parent task description")
@click.option("--subtask", "subtasks", multiple=True, help="A subtask (repeatable)")
@click.option("--subtasks-file", type=click.Path(exists=True), default=None,
              help="File with one subtask per line")
@click.option("--model", "-m", default=None, help="Model for children")
@click.option("--timeout", "timeout_s", type=int, default=None, help="Per-child timeout (seconds)")
def fleet_fanout(task, subtasks, subtasks_file, model, timeout_s) -> None:
    """Fan one task out into parallel child jobs across profiles."""
    subs = list(subtasks)
    if subtasks_file:
        subs += [ln.strip() for ln in Path(subtasks_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not subs:
        console.print("[red]Error:[/red] provide at least one --subtask or --subtasks-file")
        sys.exit(1)
    eng = _engine()
    parent = eng.fan_out(task, subs, model=model, timeout_s=timeout_s)
    console.print(f"[green]✓[/green] Orchestrator [bold]{parent.id}[/bold] "
                  f"with {len(parent.child_ids)} children: {', '.join(parent.child_ids)}")
    console.print(f"  Watch: [cyan]claudex fleet status {parent.id}[/cyan]")


@fleet_group.command("clear")
@click.option("--status", "status_filter", default=None, help="Only clear jobs in this status")
@click.option("--all", "clear_all", is_flag=True, help="Clear all terminal jobs")
@click.option("--force", is_flag=True, help="Also delete non-terminal (queued/running) jobs")
def fleet_clear(status_filter, clear_all, force) -> None:
    """Prune finished job records, logs, and results.

    By default only terminal jobs (succeeded/failed/cancelled) are removed —
    deleting a live job's record would orphan its detached worker. Use --force to
    delete non-terminal jobs anyway.
    """
    st = _parse_status(status_filter)
    if not (clear_all or status_filter):
        console.print("[yellow]Nothing to do.[/yellow] Pass --all or --status <s>.")
        return
    eng = _engine()
    n = skipped = 0
    for j in eng.list_jobs(status=st):
        if not j.is_terminal() and not force:
            skipped += 1
            continue
        eng.store.delete_job(j.id)
        n += 1
    console.print(f"[green]✓[/green] Cleared {n} job(s)")
    if skipped:
        console.print(f"[dim]Skipped {skipped} non-terminal job(s) — use --force to delete.[/dim]")


@fleet_group.command("_run-worker", hidden=True)
@click.argument("job_id")
def fleet_run_worker(job_id) -> None:
    """Internal: detached worker body. Not for direct use."""
    from claudex.fleet.runner import run_worker
    sys.exit(run_worker(job_id))


if __name__ == "__main__":
    cli()
