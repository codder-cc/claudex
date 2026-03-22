"""Settings widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, DataTable, Label, Select, Static, TextArea

from claudex.core.config import load_config
from claudex.core.profile import ProfileManager
from claudex.constants import SHAREABLE_RESOURCES


class SettingsWidget(Widget):
    BINDINGS = [
        Binding("s", "save_settings", "Save", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pm = ProfileManager()
        self._cfg = load_config()

    def compose(self) -> ComposeResult:
        profiles = self._pm.list()
        profile_options = [("(none)", "")] + [(p.name, p.name) for p in profiles]
        shell_opts = [("Auto-detect","auto"),("bash","bash"),("zsh","zsh"),
                      ("PowerShell","powershell"),("fish","fish")]
        theme_opts = [("Dark","dark"),("Light","light")]
        resume_opts = [("Env (set CLAUDE_CONFIG_DIR)","env"),
                       ("Direct (--resume flag)","direct"),
                       ("Continue (--continue)","continue")]

        with Vertical(id="settings-content"):
            yield Label("[bold]Global Settings[/bold]  [dim]s=Save[/dim]",
                        classes="settings-section-title")

            with Horizontal(classes="settings-row"):
                yield Label("Default profile: ", classes="form-label")
                yield Select(profile_options, value=self._cfg.default_profile or "",
                             id="sel-default")
            with Horizontal(classes="settings-row"):
                yield Label("Shell:           ", classes="form-label")
                yield Select(shell_opts, value=self._cfg.shell, id="sel-shell")
            with Horizontal(classes="settings-row"):
                yield Label("Theme:           ", classes="form-label")
                yield Select(theme_opts, value=self._cfg.theme, id="sel-theme")
            with Horizontal(classes="settings-row"):
                yield Label("Resume strategy: ", classes="form-label")
                yield Select(resume_opts, value=self._cfg.resume_strategy, id="sel-resume")
            yield Checkbox("Auto-switch on .claudeprofile", value=self._cfg.auto_switch,
                           id="chk-autoswitch")

            yield Label("[bold]Shell Integration[/bold]", classes="settings-section-title")
            with Horizontal():
                yield Button("Preview Script", id="btn-preview", classes="action-btn")
                yield Button("Install to Shell Profile", id="btn-install", classes="action-btn")

            yield Label("[bold]Resource Isolation[/bold]", classes="settings-section-title")
            yield Static("[dim]Shows which resources are shared vs isolated per profile[/dim]")
            yield DataTable(id="isolation-table")

            yield Label("[bold]Diagnostics[/bold]", classes="settings-section-title")
            yield Button("Run Doctor", id="btn-doctor", classes="action-btn")

    def on_mount(self) -> None:
        self._build_isolation_table()

    def _build_isolation_table(self) -> None:
        profiles = self._pm.list()
        table = self.query_one("#isolation-table", DataTable)
        table.add_column("Resource", width=20)
        for p in profiles:
            table.add_column(p.name, width=12)
        for resource in SHAREABLE_RESOURCES:
            row = [resource] + [
                "shared" if resource in p.shared_resources else "isolated"
                for p in profiles
            ]
            table.add_row(*row, key=resource)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-preview":
            self._preview_shell()
        elif event.button.id == "btn-install":
            self._install_shell()
        elif event.button.id == "btn-doctor":
            self.app.exit(("doctor",))

    def _preview_shell(self) -> None:
        try:
            from claudex.shell import get_shell_integration
            profiles = self._pm.list()
            script = get_shell_integration(self._cfg.shell).generate_init_script(profiles)
            self.app.push_screen(ScriptPreviewModal(script))
        except Exception as e:
            self.notify(str(e), severity="error")

    def _install_shell(self) -> None:
        try:
            from claudex.shell import get_shell_integration
            profiles = self._pm.list()
            integration = get_shell_integration(self._cfg.shell)
            installed_to = integration.install(profiles)
            self.notify(f"Installed to {installed_to}\nRestart shell to apply.", timeout=5)
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_save_settings(self) -> None:
        try:
            default = self.query_one("#sel-default", Select).value
            shell = self.query_one("#sel-shell", Select).value
            theme = self.query_one("#sel-theme", Select).value
            resume = self.query_one("#sel-resume", Select).value
            auto_switch = self.query_one("#chk-autoswitch", Checkbox).value
            self._cfg.set("default_profile", str(default) if default else "")
            self._cfg.set("shell", str(shell))
            self._cfg.set("theme", str(theme))
            self._cfg.set("resume_strategy", str(resume))
            self._cfg.set("auto_switch", auto_switch)
            self._cfg.save()
            self.notify("Settings saved.")
        except Exception as e:
            self.notify(str(e), severity="error")


class ScriptPreviewModal(ModalScreen):
    def __init__(self, script: str) -> None:
        super().__init__()
        self._script = script

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            yield Static("[bold]Shell Script Preview[/bold]", id="dialog-title")
            yield TextArea(self._script, id="script-area", read_only=True)
            with Horizontal(id="dialog-buttons"):
                yield Button("Close", id="btn-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
