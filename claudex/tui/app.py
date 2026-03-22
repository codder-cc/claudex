"""Textual application root."""

from __future__ import annotations

import os
import subprocess

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from claudex.tui.theme import APP_CSS
from claudex.tui.screens.dashboard import DashboardWidget
from claudex.tui.screens.history import HistoryWidget
from claudex.tui.screens.auth_manager import AuthManagerWidget
from claudex.tui.screens.settings import SettingsWidget
from claudex.constants import CLAUDE_CONFIG_DIR_ENV, CLAUDE_BIN


class ClaudexApp(App):
    CSS = APP_CSS
    TITLE = "claudex"
    SUB_TITLE = "Claude Code profile manager"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("1", "show_tab('profiles')", "Profiles"),
        Binding("2", "show_tab('history')", "History"),
        Binding("3", "show_tab('auth')", "Auth"),
        Binding("4", "show_tab('settings')", "Settings"),
        Binding("?", "help", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="main-tabs", initial="profiles"):
            with TabPane("Profiles [dim](1)[/dim]", id="profiles"):
                yield DashboardWidget()
            with TabPane("History [dim](2)[/dim]", id="history"):
                yield HistoryWidget()
            with TabPane("Auth [dim](3)[/dim]", id="auth"):
                yield AuthManagerWidget()
            with TabPane("Settings [dim](4)[/dim]", id="settings"):
                yield SettingsWidget()
        yield Footer()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_help(self) -> None:
        self.notify(
            "[bold]Navigation[/bold]\n"
            "  1-4      Switch tabs\n"
            "  q        Quit\n"
            "\n"
            "[bold]Profiles tab[/bold]\n"
            "  n        New profile\n"
            "  Enter    Switch to profile (sets CLAUDE_CONFIG_DIR)\n"
            "  l        Launch claude with this profile right now\n"
            "  d        Delete profile\n"
            "  r        Refresh list\n"
            "\n"
            "[bold]Auth tab[/bold]\n"
            "  Import Current Login  — copies your active ~/.claude session\n"
            "                          (use this if you're already logged in)\n"
            "  a / Add OAuth Login   — runs 'claude /login' for a fresh login\n"
            "  k / Add API Key       — paste a sk-ant-api03-... key\n"
            "  v / Revoke            — clear stored credentials\n"
            "\n"
            "[bold]History tab[/bold]\n"
            "  Enter    Resume selected session\n"
            "  m        Migrate session to another profile\n"
            "  x        Delete session\n"
            "  /        Focus search box\n"
            "\n"
            "[bold]Settings tab[/bold]\n"
            "  s        Save settings\n"
            "\n"
            "[bold]CLI quick reference[/bold]\n"
            "  claudex auth import-current <name>  — import current login\n"
            "  claudex auth add <name>             — fresh OAuth login\n"
            "  claudex auth key <name>             — add API key\n"
            "  claudex auth status                 — show all token expiry",
            title="claudex Help  (?)",
            timeout=20,
        )

    def on_mount(self) -> None:
        from claudex.core.config import load_config
        cfg = load_config()
        if cfg.theme == "light":
            self.dark = False


def run_app() -> None:
    """Launch the TUI and handle post-exit actions."""
    result = ClaudexApp().run()

    if not result:
        return

    action = result[0] if result else None

    if action == "launch":
        _, profile_name, config_dir = result
        env = {**os.environ, CLAUDE_CONFIG_DIR_ENV: str(config_dir)}
        try:
            subprocess.run([CLAUDE_BIN], env=env)
        except FileNotFoundError:
            print(f"Claude CLI not found. Set CLAUDE_CONFIG_DIR={config_dir} and run 'claude'")

    elif action == "resume":
        _, profile_name, config_dir, session_id = result
        env = {**os.environ, CLAUDE_CONFIG_DIR_ENV: str(config_dir)}
        cmd = [CLAUDE_BIN, "--resume", session_id] if session_id else [CLAUDE_BIN]
        try:
            subprocess.run(cmd, env=env)
        except FileNotFoundError:
            print(f"Claude CLI not found. Set CLAUDE_CONFIG_DIR={config_dir} and run 'claude'")

    elif action == "auth_login":
        _, profile_name, config_dir = result
        env = {**os.environ, CLAUDE_CONFIG_DIR_ENV: str(config_dir)}
        try:
            subprocess.run([CLAUDE_BIN, "/login"], env=env)
            from claudex.core.auth import AuthManager
            AuthManager()._import_claude_credentials(profile_name, config_dir)
            print(f"Auth completed for profile '{profile_name}'")
        except FileNotFoundError:
            print("Claude CLI not found.")

    elif action == "doctor":
        from claudex.commands.doctor import run_doctor
        run_doctor()
