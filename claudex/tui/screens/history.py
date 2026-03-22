"""History browser widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, ListItem, ListView, Select, Static

from claudex.core.profile import ProfileManager
from claudex.history.browser import HistoryBrowser
from claudex.history.models import Session


class HistoryWidget(Widget):
    BINDINGS = [
        Binding("enter", "resume_session", "Resume", show=True),
        Binding("m", "migrate_session", "Migrate", show=True),
        Binding("x", "delete_session", "Delete", show=True),
        Binding("/", "focus_search", "Search", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pm = ProfileManager()
        self._browser: HistoryBrowser | None = None
        self._sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="search-bar"):
                yield Input(placeholder="Search sessions... (press /)", id="search-input")
                yield Select(
                    [("All profiles", "")],
                    value="",
                    id="profile-filter",
                    allow_blank=False,
                )
            yield DataTable(id="session-table", cursor_type="row")
            yield Static("", id="session-detail")

    def on_mount(self) -> None:
        self._setup()

    def _setup(self) -> None:
        profiles = self._pm.list()
        self._browser = HistoryBrowser(profiles)

        selector = self.query_one("#profile-filter", Select)
        selector.set_options([("All profiles", "")] + [(p.name, p.name) for p in profiles])

        table = self.query_one("#session-table", DataTable)
        table.add_columns("Profile", "Last Active", "Title", "Msgs", "Tokens", "Project")
        self._load_sessions()

    def _load_sessions(self, query: str = "", profile_filter: str = "") -> None:
        if not self._browser:
            return
        if query:
            sessions = self._browser.search(query, profile_filter=profile_filter or None)
        else:
            sessions = self._browser.get_all_sessions(
                profile_filter=profile_filter or None, limit=200)
        self._sessions = sessions
        table = self.query_one("#session-table", DataTable)
        table.clear()
        for s in sessions:
            table.add_row(
                s.profile_name, s.age_human, s.title[:60],
                str(s.message_count), f"{s.total_tokens.total:,}",
                str(s.project_path)[-35:],
                key=s.session_id,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            selector = self.query_one("#profile-filter", Select)
            self._load_sessions(event.value, str(selector.value) if selector.value else "")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "profile-filter":
            search = self.query_one("#search-input", Input).value
            self._load_sessions(search, str(event.value) if event.value else "")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key:
            return
        session = next((s for s in self._sessions if s.session_id == event.row_key.value), None)
        if session:
            self.query_one("#session-detail", Static).update(
                f"[bold]{session.title}[/bold]  |  "
                f"Profile: {session.profile_name}  |  "
                f"Msgs: {session.message_count}  |  "
                f"Tokens: {session.total_tokens.total:,}  |  "
                f"Project: {session.project_path}"
            )

    def _get_selected_session(self) -> Session | None:
        table = self.query_one("#session-table", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self._sessions):
            return None
        return self._sessions[table.cursor_row]

    def action_resume_session(self) -> None:
        session = self._get_selected_session()
        if not session:
            return
        try:
            profile = self._pm.get(session.profile_name)
        except Exception:
            self.notify(f"Profile '{session.profile_name}' not found", severity="error")
            return
        self.app.exit(("resume", session.profile_name, profile.config_dir, session.session_id))

    def action_migrate_session(self) -> None:
        session = self._get_selected_session()
        if not session:
            return
        other = [p for p in self._pm.list() if p.name != session.profile_name]
        if not other:
            self.notify("No other profiles to migrate to", severity="warning")
            return
        self.app.push_screen(
            MigrateModal(session, other),
            lambda result: self._do_migrate(session, result) if result else None,
        )

    def _do_migrate(self, session: Session, to_profile_name: str) -> None:
        if not self._browser:
            return
        try:
            profile = self._pm.get(to_profile_name)
            self._browser.migrate_session(session, to_profile_name, profile.config_dir)
            self._load_sessions()
            self.notify(f"Session migrated to '{to_profile_name}'")
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_delete_session(self) -> None:
        session = self._get_selected_session()
        if not session:
            return
        from claudex.tui.screens.dashboard import ConfirmModal
        self.app.push_screen(
            ConfirmModal(f"Delete session?", f"'{session.title[:50]}'"),
            lambda ok: self._do_delete(session) if ok else None,
        )

    def _do_delete(self, session: Session) -> None:
        if self._browser:
            self._browser.delete_session(session)
        if session in self._sessions:
            self._sessions.remove(session)
        self._load_sessions()
        self.notify("Session deleted.")

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()


class MigrateModal(ModalScreen):
    def __init__(self, session: Session, profiles: list) -> None:
        super().__init__()
        self._session = session
        self._profiles = profiles

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            yield Static("[bold]Migrate session to:[/bold]", id="dialog-title")
            yield Static(f"[dim]{self._session.title[:60]}[/dim]")
            yield ListView(
                *[ListItem(Label(p.name), id=f"p-{p.name}") for p in self._profiles],
                id="migrate-list",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="btn-cancel")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id:
            self.dismiss(event.item.id.replace("p-", "", 1))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
