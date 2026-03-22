"""Auth manager widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from claudex.core.auth import AuthManager, AuthStatus
from claudex.core.profile import Profile, ProfileManager


AUTH_LABELS = {
    "oauth":   "[green]● OAuth[/green]",
    "api_key": "[yellow]● API Key[/yellow]",
    "none":    "[red]✗ None[/red]",
}


class AuthManagerWidget(Widget):
    BINDINGS = [
        Binding("a", "add_oauth", "OAuth Login", show=True),
        Binding("k", "add_api_key", "API Key", show=True),
        Binding("v", "revoke", "Revoke", show=True),
        Binding("r", "refresh_list", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pm = ProfileManager()
        self._auth = AuthManager()
        self._profiles: list[Profile] = []
        self._statuses: dict[str, AuthStatus] = {}
        self._selected: Profile | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="auth-left"):
                yield Label("[bold] Auth Manager[/bold]  [dim]a=OAuth  k=APIKey  v=Revoke[/dim]")
                yield ListView(id="auth-list")
            with Vertical(id="auth-detail"):
                yield Static("Select a profile", id="auth-detail-title")
                yield Static("", id="auth-detail-body")
                with Horizontal(id="auth-actions"):
                    yield Button("Add OAuth Login", id="btn-oauth", classes="action-btn")
                    yield Button("Add API Key", id="btn-apikey", classes="action-btn")
                    yield Button("Import Current Login", id="btn-import", classes="action-btn")
                    yield Button("Revoke / Clear", id="btn-revoke", variant="error", classes="action-btn")

    def on_mount(self) -> None:
        self._load_profiles()

    def _load_profiles(self) -> None:
        self._profiles = self._pm.list()
        lv = self.query_one("#auth-list", ListView)
        lv.clear()
        for profile in self._profiles:
            try:
                status = self._auth.get_status(profile.name, profile.config_dir)
                self._statuses[profile.name] = status
                label_str = AUTH_LABELS.get(status.auth_type, AUTH_LABELS["none"])
                expiry = f" ({status.expires_in_human})" if status.auth_type == "oauth" else ""
            except Exception:
                label_str = AUTH_LABELS["none"]
                expiry = ""
            lv.append(ListItem(Label(f"{label_str} {profile.name}{expiry}"), id=f"p-{profile.name}"))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if not event.item or not event.item.id:
            return
        name = event.item.id.replace("p-", "", 1)
        profile = next((p for p in self._profiles if p.name == name), None)
        if profile:
            self._selected = profile
            self._update_detail(profile)

    def _update_detail(self, profile: Profile) -> None:
        self.query_one("#auth-detail-title", Static).update(f"[bold]{profile.name}[/bold]")
        status = self._statuses.get(profile.name)
        if not status:
            self.query_one("#auth-detail-body", Static).update("[dim]No status[/dim]")
            return
        lines = [
            f"[dim]Type:[/dim]      {status.auth_type}",
            f"[dim]Email:[/dim]     {status.email or '—'}",
            f"[dim]Expires:[/dim]   {status.expires_in_human}",
            f"[dim]Refresh:[/dim]   {'Yes' if status.refresh_available else 'No'}",
        ]
        if status.raw_token_preview:
            lines.append(f"[dim]Token:[/dim]     {status.raw_token_preview}")
        if status.is_expired:
            lines.append("\n[red bold]TOKEN EXPIRED — re-authenticate[/red bold]")
        self.query_one("#auth-detail-body", Static).update("\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "btn-oauth":  self.action_add_oauth,
            "btn-apikey": self.action_add_api_key,
            "btn-import": self._import_current,
            "btn-revoke": self.action_revoke,
        }
        fn = handlers.get(event.button.id)
        if fn:
            fn()

    def action_add_oauth(self) -> None:
        if not self._selected:
            self.notify("Select a profile first", severity="warning")
            return
        self.app.exit(("auth_login", self._selected.name, self._selected.config_dir))

    def action_add_api_key(self) -> None:
        if not self._selected:
            self.notify("Select a profile first", severity="warning")
            return
        self.app.push_screen(ApiKeyModal(self._selected.name),
                              lambda key: self._store_api_key(key) if key else None)

    def _import_current(self) -> None:
        if not self._selected:
            self.notify("Select a profile first", severity="warning")
            return
        import os
        from pathlib import Path
        profile = self._selected
        default_claude = Path.home() / ".claude"
        candidates = [
            Path(os.environ.get("CLAUDE_CONFIG_DIR", "")) / ".credentials.json",
            default_claude / ".credentials.json",
            Path.home() / ".claude.json",
        ]
        import json as _json
        import shutil
        for cred_file in candidates:
            if cred_file.exists():
                dest = profile.config_dir / ".credentials.json"
                shutil.copy2(str(cred_file), str(dest))
                # Best-effort: store in keyring (may fail; credentials file is sufficient)
                try:
                    self._auth._import_claude_credentials(profile.name, profile.config_dir)
                except Exception:
                    pass
                status = self._auth.get_status(profile.name, profile.config_dir)
                profile.auth_type = status.auth_type if status.auth_type != "none" else "oauth"
                profile.save()
                # Seed .claude.json so interactive Claude skips the auth selector
                self._seed_claude_json(profile.config_dir)
                self._load_profiles()
                self.notify(f"Imported session — expires: {status.expires_in_human}")
                return
        self.notify("No active Claude session found", severity="warning")

    def _store_api_key(self, api_key: str) -> None:
        if not self._selected:
            return
        try:
            self._auth.add_api_key(self._selected.name, api_key)
            profile = self._pm.get(self._selected.name)
            profile.auth_type = "api_key"
            profile.save()
            self._load_profiles()
            self.notify(f"API key stored for '{self._selected.name}'")
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_revoke(self) -> None:
        if not self._selected:
            return
        from claudex.tui.screens.dashboard import ConfirmModal
        profile = self._selected
        self.app.push_screen(
            ConfirmModal(f"Revoke auth for '{profile.name}'?", "Removes stored credentials."),
            lambda ok: self._do_revoke(profile.name) if ok else None,
        )

    def _seed_claude_json(self, config_dir) -> None:
        """Seed .claude.json in profile dir so interactive Claude skips the auth selector."""
        import json as _json
        from pathlib import Path as _Path
        home_claude_json = _Path.home() / ".claude.json"
        dest = config_dir / ".claude.json"
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
            return
        existing: dict = {}
        if dest.exists():
            try:
                existing = _json.loads(dest.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        merged = {**seed_data, **existing}
        dest.write_text(_json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    def _do_revoke(self, name: str) -> None:
        self._auth.revoke(name)
        try:
            p = self._pm.get(name)
            p.auth_type = "none"
            p.save()
        except Exception:
            pass
        self._load_profiles()
        self.notify(f"Auth revoked for '{name}'")

    def action_refresh_list(self) -> None:
        self._load_profiles()
        self.notify("Refreshed", timeout=1)


class ApiKeyModal(ModalScreen):
    def __init__(self, profile_name: str) -> None:
        super().__init__()
        self._profile_name = profile_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            yield Static(f"[bold]Add API Key — {self._profile_name}[/bold]", id="dialog-title")
            yield Label("Paste your Anthropic API key (sk-ant-...):", classes="form-label")
            yield Input(placeholder="sk-ant-api03-...", password=True, id="key-input", classes="form-input")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        key = self.query_one("#key-input", Input).value.strip()
        if not key:
            self.notify("API key is required", severity="error")
            return
        self.dismiss(key)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
