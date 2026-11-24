"""Message-level transcript editor — view, edit, delete, trim, live-follow.

Opened for a single session from the History tab (or via ``claudex history
edit``). The left pane lists every message on the session's active chain; the
right pane shows the selected message in full. Editing changes a message's text
in place; deleting splices the chain around the gap (a tool_use and its result
go together); "trim to here" drops everything after the cursor. All writes go
through :mod:`claudex.history.edit`, which backs the file up and replaces it
atomically.

Live-follow (``f``) tails the file for appends — handy for watching a running
session. Note that editing a transcript a live ``claude`` process is actively
using does not change what that process sends next (it already holds the
conversation in memory); the edit takes effect when the session is next
``--resume``d. The editor says so while follow is on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, ListItem, ListView, Static, TextArea

from claudex.history import edit
from claudex.history.models import Session

_KIND_STYLE = {
    "text": "white",
    "thinking": "magenta",
    "tool_use": "yellow",
    "tool_result": "cyan",
    "command": "bright_black",
    "meta": "bright_black",
    "error": "red",
}


class MessageEditorScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
        Binding("e", "edit_msg", "Edit text", show=True),
        Binding("x", "delete_msg", "Delete", show=True),
        Binding("t", "trim_here", "Trim to here", show=True),
        Binding("a", "toggle_all", "All / turns", show=True),
        Binding("f", "toggle_follow", "Follow", show=True),
        Binding("r", "reload", "Reload", show=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session
        self._path: Path = session.file_path
        self._msgs: List[edit.MsgView] = []
        self._mode = "turns"      # "turns" (real conversation) | "all" (every entry)
        self._loading = False
        self._follow = False
        self._offset = 0
        self._timer = None

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="editor-header")
            with Horizontal(id="editor-body"):
                yield ListView(id="msg-list")
                with VerticalScroll(id="msg-content"):
                    yield Static("", id="msg-content-body")
            yield Static(
                "[dim]e edit · x delete · t trim-to-here · a all/turns · "
                "f follow · r reload · esc back[/dim]",
                id="editor-hints",
            )

    def on_mount(self) -> None:
        try:
            self._offset = self._path.stat().st_size
        except OSError:
            self._offset = 0
        self._reload()

    # ── data (loaded off the UI thread — transcripts can be hundreds of MB) ─────
    def _reload(self) -> None:
        self._load_worker()

    @work(exclusive=True)
    async def _load_worker(self) -> None:
        self._loading = True
        self._update_header()
        try:
            self._msgs = await asyncio.to_thread(
                edit.list_messages, self._path, turns=(self._mode == "turns"))
        except OSError as e:
            self._msgs = []
            self.notify(f"Can't read transcript: {e}", severity="error")
        self._loading = False
        self._populate()

    def _populate(self) -> None:
        lv = self.query_one("#msg-list", ListView)
        idx = lv.index or 0
        lv.clear()
        for m in self._msgs:
            style = _KIND_STYLE.get(m.kind, "white")
            rnd = f"r{m.round_index + 1}" if m.round_index >= 0 else "  "
            lock = "" if m.editable else " [dim]🔒[/dim]"
            label = (f"[dim]{rnd}[/dim] [{style}]{m.role[:4]:4}[/] "
                     f"[{style}]{m.kind:11}[/]{lock}  {m.preview}")
            lv.append(ListItem(Label(label)))
        if self._msgs:
            lv.index = min(idx, len(self._msgs) - 1)
        self._update_header()
        self._show_selected()

    def _update_header(self) -> None:
        if self._loading:
            state = "  [yellow]⏳ loading…[/]"
        elif self._follow:
            state = "  [b green]● LIVE — edits apply on next resume[/]"
        else:
            state = ""
        mode = "turns" if self._mode == "turns" else "all"
        self.query_one("#editor-header", Static).update(
            f"[b]{self._session.title[:60]}[/b]  "
            f"[dim]{self._session.profile_name} · {len(self._msgs)} {mode} · "
            f"{self._session.session_id[:12]}…[/dim]{state}"
        )

    def _selected(self) -> Optional[edit.MsgView]:
        lv = self.query_one("#msg-list", ListView)
        if lv.index is None or not (0 <= lv.index < len(self._msgs)):
            return None
        return self._msgs[lv.index]

    def _show_selected(self) -> None:
        m = self._selected()
        body = self.query_one("#msg-content-body", Static)
        if not m:
            body.update("")
            return
        head = f"{m.role} · {m.kind} · {m.uuid[:8]}\n{'─' * 40}\n"
        body.update(Text(head) + Text(m.text or "(empty)"))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._show_selected()

    # ── navigation ──────────────────────────────────────────────────────────────
    def action_cursor_down(self) -> None:
        self.query_one("#msg-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#msg-list", ListView).action_cursor_up()

    def action_back(self) -> None:
        self._stop_follow()
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._reload()

    def action_toggle_all(self) -> None:
        self._mode = "all" if self._mode == "turns" else "turns"
        self._reload()
        self.notify("Showing every entry." if self._mode == "all"
                    else "Showing the conversation (turns).")

    # ── edits ───────────────────────────────────────────────────────────────────
    def action_edit_msg(self) -> None:
        m = self._selected()
        if not m:
            return
        if not m.editable:
            self.notify(f"{m.kind} messages aren't text-editable", severity="warning")
            return
        self.app.push_screen(
            EditTextModal(m.text, f"{m.role} · {m.kind} · {m.uuid[:8]}"),
            lambda new: self._do_edit(m.uuid, new) if new is not None else None,
        )

    def _do_edit(self, uuid: str, new_text: str) -> None:
        try:
            edit.edit_message_text(self._path, uuid, new_text)
        except edit.TrimError as e:
            self.notify(str(e), severity="error")
            return
        self._reload()
        self.notify("Message edited (backup written).")

    def action_delete_msg(self) -> None:
        m = self._selected()
        if not m:
            return
        from claudex.tui.screens.dashboard import ConfirmModal
        note = m.preview or m.kind
        if m.kind in ("tool_use", "tool_result"):
            note += "  (its tool pair will go too)"
        self.app.push_screen(
            ConfirmModal("Delete this message?", note),
            lambda ok: self._do_delete(m.uuid) if ok else None,
        )

    def _do_delete(self, uuid: str) -> None:
        try:
            edit.delete_message(self._path, uuid)
        except edit.TrimError as e:
            self.notify(str(e), severity="error")
            return
        self._reload()
        self.notify("Message deleted (backup written).")

    def action_trim_here(self) -> None:
        m = self._selected()
        if not m:
            return
        from claudex.tui.screens.dashboard import ConfirmModal
        self.app.push_screen(
            ConfirmModal("Trim everything after this message?",
                         f"keeps through: {m.preview or m.kind}"),
            lambda ok: self._do_trim(m.uuid) if ok else None,
        )

    def _do_trim(self, uuid: str) -> None:
        try:
            bak = edit.truncate_to(self._path, uuid)
        except edit.TrimError as e:
            self.notify(str(e), severity="error")
            return
        self._reload()
        self.notify("Trimmed." if bak else "Already the last message — nothing to trim.")

    # ── live follow ─────────────────────────────────────────────────────────────
    def action_toggle_follow(self) -> None:
        if self._follow:
            self._stop_follow()
        else:
            self._follow = True
            self._timer = self.set_interval(1.0, self._poll)
        self._update_header()

    def _stop_follow(self) -> None:
        self._follow = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _poll(self) -> None:
        try:
            new, self._offset = edit.read_since(self._path, self._offset)
        except OSError:
            return
        if new:
            self._reload()
            lv = self.query_one("#msg-list", ListView)
            if self._msgs:
                lv.index = len(self._msgs) - 1   # jump to newest


class EditTextModal(ModalScreen):
    """A multi-line editor for a single message's text."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, text: str, title: str) -> None:
        super().__init__()
        self._text = text
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Static(f"[bold]Edit message[/bold]  [dim]{self._title}[/dim]",
                         id="dialog-title")
            yield TextArea(self._text, id="edit-area")
            yield Static("[dim]ctrl+s save · esc cancel[/dim]")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="success", id="btn-save")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#edit-area", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#edit-area", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        else:
            self.dismiss(None)
