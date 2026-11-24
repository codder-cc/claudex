"""Pilot tests for the TUI message editor screen."""

import json
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import ListView

from claudex.history import edit
from claudex.history.models import Session
from claudex.tui.theme import APP_CSS
from claudex.tui.screens.message_editor import MessageEditorScreen


def _line(obj):
    return json.dumps(obj) + "\n"


@pytest.fixture
def session(tmp_path) -> Session:
    f = tmp_path / "sess.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "first question"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1",
         "message": {"role": "user", "content": "second question"}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "second answer"}]}},
    ]
    f.write_text("".join(_line(r) for r in rows))
    return Session(session_id="sess", project_path=tmp_path, profile_name="test", file_path=f)


class _Host(App):
    CSS = APP_CSS

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session

    def on_mount(self) -> None:
        self.push_screen(MessageEditorScreen(self._session))


async def _settle(app, pilot):
    """Wait for the background load worker to finish, then flush the UI."""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_editor_mounts_and_lists_messages(session):
    app = _Host(session)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, MessageEditorScreen)
        lv = screen.query_one("#msg-list", ListView)
        assert len(lv.children) == 4
        assert screen._selected().uuid == "u1"


async def test_editor_turns_mode_hides_tool_noise(tmp_path):
    f = tmp_path / "noisy.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "do it"}},
        {"type": "assistant", "uuid": "t1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]}},
        {"type": "user", "uuid": "r1", "parentUuid": "t1", "toolUseResult": {"ok": 1},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "r1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    ]
    f.write_text("".join(_line(r) for r in rows))
    s = Session(session_id="n", project_path=tmp_path, profile_name="t", file_path=f)
    app = _Host(s)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        # turns mode: only the user turn + the assistant text reply
        assert [m.uuid for m in screen._msgs] == ["u1", "a1"]
        screen.action_toggle_all()
        await _settle(app, pilot)
        assert [m.uuid for m in screen._msgs] == ["u1", "t1", "r1", "a1"]


async def test_editor_edit_updates_file(session):
    app = _Host(session)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        screen._do_edit("u1", "PILOT EDITED")
        await _settle(app, pilot)
        assert any(m.uuid == "u1" and m.text == "PILOT EDITED" for m in screen._msgs)
    objs = [json.loads(l) for l in session.file_path.read_text().splitlines() if l.strip()]
    u1 = next(o for o in objs if o.get("uuid") == "u1")
    assert u1["message"]["content"] == "PILOT EDITED"


async def test_editor_trim_to_here(session):
    app = _Host(session)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        screen._do_trim("a1")        # keep through first answer
        await _settle(app, pilot)
        assert [m.uuid for m in screen._msgs] == ["u1", "a1"]


async def test_editor_delete_message(session):
    app = _Host(session)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        screen._do_delete("u2")
        await _settle(app, pilot)
        uuids = [m.uuid for m in screen._msgs]
        assert "u2" not in uuids and "a2" in uuids   # a2 re-linked, survives


async def test_editor_follow_toggle(session):
    app = _Host(session)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        assert screen._follow is False
        screen.action_toggle_follow()
        assert screen._follow is True and screen._timer is not None
        screen.action_toggle_follow()
        assert screen._follow is False and screen._timer is None


async def test_history_tab_survives_duplicate_session_ids(tmp_path, monkeypatch):
    """Regression: the same session id under two profiles must not crash the
    DataTable (its row key is now profile-qualified)."""
    from textual.widgets import DataTable
    from claudex.tui.screens import history as history_mod
    from claudex.tui.screens.history import HistoryWidget

    dup = [
        Session(session_id="same-id", project_path=tmp_path, profile_name="work",
                file_path=tmp_path / "a.jsonl"),
        Session(session_id="same-id", project_path=tmp_path, profile_name="personal",
                file_path=tmp_path / "b.jsonl"),
    ]

    class _FakeBrowser:
        def __init__(self, *_a, **_k): pass
        def get_all_sessions(self, *_a, **_k): return dup
        def search(self, *_a, **_k): return dup

    # Swap the browser (no disk scan) but let the real widget mount normally.
    monkeypatch.setattr(history_mod, "HistoryBrowser", _FakeBrowser)

    class _H(App):
        CSS = APP_CSS
        def compose(self):
            yield HistoryWidget()

    app = _H()
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()   # the list loads in a worker now
        await pilot.pause()
        table = app.query_one("#session-table", DataTable)
        assert table.row_count == 2       # both rows added, no DuplicateKey crash


async def test_editor_insert_and_replace_and_delete_round(session):
    app = _Host(session)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        screen = app.screen
        # insert a user message after a1
        screen._do_insert("a1", ("user", "steer it"))
        await _settle(app, pilot)
        assert any(m.text == "steer it" and m.role == "user" for m in screen._msgs)
        # find/replace
        screen._do_replace(("second question", "SECOND Q"))
        await _settle(app, pilot)
        assert any(m.text == "SECOND Q" for m in screen._msgs)
        # delete round 0 (the first exchange)
        screen._do_delete_round(0)
        await _settle(app, pilot)
        assert all(m.uuid != "u1" for m in screen._msgs)


async def test_editor_delete_errors_action(tmp_path):
    f = tmp_path / "e.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "e1", "parentUuid": "u1", "isApiErrorMessage": True,
         "message": {"role": "assistant", "model": "<synthetic>",
                     "content": [{"type": "text", "text": "API Error: safeguards"}]}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "e1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    s = Session(session_id="e", project_path=tmp_path, profile_name="t", file_path=f)
    app = _Host(s)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        app.screen.action_toggle_all()        # error only visible in 'all'
        await _settle(app, pilot)
        assert any(m.kind == "error" for m in app.screen._msgs)
        app.screen._do_delete_errors()
        await _settle(app, pilot)
        assert all(m.kind != "error" for m in app.screen._msgs)
