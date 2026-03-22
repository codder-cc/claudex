"""Dashboard widget — profile list with auth status."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, Static

from claudex.core.auth import AuthManager
from claudex.core.profile import Profile, ProfileManager
from claudex.constants import CLAUDE_CONFIG_DIR_ENV


AUTH_ICONS = {
    "oauth":   ("● OAuth  ", "auth-ok"),
    "api_key": ("● APIKey ", "auth-api"),
    "none":    ("✗ None   ", "auth-none"),
}


class ProfileDetailPanel(Static):
    def update_profile(self, profile: Profile, auth_status) -> None:
        lines = [
            f"[bold cyan]{profile.name}[/bold cyan]\n",
            f"[dim]Email:[/dim]    {profile.email or '—'}",
            f"[dim]Config:[/dim]   {profile.config_dir}",
            f"[dim]Aliases:[/dim]  {', '.join(profile.aliases) or '—'}",
            f"[dim]Created:[/dim]  {profile.created_at.strftime('%Y-%m-%d')}",
        ]
        if auth_status:
            lines += [
                "",
                f"[dim]Auth:[/dim]     {auth_status.auth_type}",
                f"[dim]Expires:[/dim]  {auth_status.expires_in_human}",
            ]
        if profile.notes:
            lines.append(f"\n[dim]Notes:[/dim] {profile.notes}")
        self.update("\n".join(lines))


class DashboardWidget(Widget):
    BINDINGS = [
        Binding("n", "new_profile", "New", show=True),
        Binding("d", "delete_profile", "Delete", show=True),
        Binding("enter", "switch_profile", "Switch", show=True),
        Binding("l", "launch_profile", "Launch", show=True),
        Binding("r", "refresh_list", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pm = ProfileManager()
        self._auth = AuthManager()
        self._profiles: list[Profile] = []
        self._statuses: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Label(" [bold]Profiles[/bold]   [dim]n=New  d=Delete  Enter=Switch  l=Launch[/dim]")
                yield DataTable(id="profile-table", cursor_type="row")
            yield ProfileDetailPanel("Select a profile", id="detail-panel")

    def on_mount(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        table.add_columns(" ", "Name", "Auth", "Last Used", "Sessions", "Expires")
        self._load_profiles()

    def _load_profiles(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        table.clear()
        self._profiles = self._pm.list()
        active = self._pm.get_active()

        for profile in self._profiles:
            try:
                status = self._auth.get_status(profile.name, profile.config_dir)
                self._statuses[profile.name] = status
            except Exception:
                status = None

            is_active = active == profile.name or str(profile.config_dir) in active
            marker = "▶" if is_active else " "
            auth_icon, _ = AUTH_ICONS.get(getattr(status, "auth_type", "none"), AUTH_ICONS["none"])

            last = "never"
            if profile.last_used:
                try:
                    import humanize
                    last = humanize.naturaltime(profile.last_used)
                except Exception:
                    delta = datetime.now() - profile.last_used
                    last = f"{int(delta.total_seconds() // 3600)}h ago"

            sessions = sum(1 for _ in (profile.config_dir / "projects").rglob("*.jsonl")) \
                if (profile.config_dir / "projects").exists() else 0
            expires = getattr(status, "expires_in_human", "—") if status else "—"

            table.add_row(marker, profile.name, auth_icon, last, str(sessions), expires,
                          key=profile.name)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key:
            return
        profile = next((p for p in self._profiles if p.name == event.row_key.value), None)
        if profile:
            status = self._statuses.get(profile.name)
            self.query_one("#detail-panel", ProfileDetailPanel).update_profile(profile, status)

    def action_switch_profile(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        if table.cursor_row < 0 or not self._profiles:
            return
        profile = self._profiles[table.cursor_row]
        self._pm.set_active(profile.name)
        self.notify(
            f"Profile '{profile.name}' activated.\nRun: source ~/.claudex/.current_env",
            title="Switched",
        )
        self._load_profiles()

    def action_launch_profile(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        if table.cursor_row < 0 or not self._profiles:
            return
        profile = self._profiles[table.cursor_row]
        self.app.exit(("launch", profile.name, profile.config_dir))

    def action_refresh_list(self) -> None:
        self._load_profiles()
        self.notify("Refreshed", timeout=1)

    def action_new_profile(self) -> None:
        self.app.push_screen(NewProfileModal(self._pm), self._on_profile_created)

    def _on_profile_created(self, result) -> None:
        if result:
            self._load_profiles()

    def action_delete_profile(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        if table.cursor_row < 0 or not self._profiles:
            return
        profile = self._profiles[table.cursor_row]
        self.app.push_screen(
            ConfirmModal(f"Delete profile '{profile.name}'?",
                         "Removes the profile marker but keeps history."),
            lambda ok: self._do_delete(profile.name) if ok else None,
        )

    def _do_delete(self, name: str) -> None:
        self._pm.delete(name)
        self._load_profiles()
        self.notify(f"Profile '{name}' deleted.")


# ── Modal dialogs (these ARE Screen subclasses — they overlay everything) ─────

class NewProfileModal(ModalScreen):
    def __init__(self, pm: ProfileManager) -> None:
        super().__init__()
        self._pm = pm

    def compose(self) -> ComposeResult:
        with Vertical(id="new-profile-form"):
            yield Static("[bold]New Profile[/bold]", id="dialog-title")
            yield Label("Name:", classes="form-label")
            yield Input(placeholder="e.g. work", id="input-name", classes="form-input")
            yield Label("Email (optional):", classes="form-label")
            yield Input(placeholder="you@example.com", id="input-email", classes="form-input")
            yield Label("Aliases (comma-separated):", classes="form-label")
            yield Input(placeholder="claude-work, work", id="input-aliases", classes="form-input")
            with Horizontal(id="dialog-buttons"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        name = self.query_one("#input-name", Input).value.strip()
        email = self.query_one("#input-email", Input).value.strip()
        aliases_raw = self.query_one("#input-aliases", Input).value.strip()
        aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()] if aliases_raw else []
        if not name:
            self.notify("Name is required", severity="error")
            return
        try:
            self._pm.create(name, email=email, aliases=aliases or None)
            self.dismiss(name)
        except Exception as e:
            self.notify(str(e), severity="error")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__()
        self._message = message
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            yield Static(f"[bold]{self._message}[/bold]", id="dialog-title")
            if self._detail:
                yield Static(self._detail)
            with Horizontal(id="dialog-buttons"):
                yield Button("Confirm", variant="error", id="btn-confirm")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
