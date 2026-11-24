"""Cached editor message index + incremental growth."""

import json

from claudex.history import edit, msgindex


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _rows(n_start=0):
    return [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "first"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "reply one"}]}},
    ]


def test_index_cold_warm_matches_full_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(msgindex, "_DIR", tmp_path / "cache")
    f = tmp_path / "s.jsonl"
    _w(f, _rows())
    cold = msgindex.list_views(f)
    warm = msgindex.list_views(f)
    assert [m.uuid for m in cold] == [m.uuid for m in warm] == ["u1", "a1"]
    # matches the authoritative full scan
    full = edit.list_messages(f)
    assert [m.uuid for m in full] == [m.uuid for m in cold]
    # offsets let us read full text lazily
    for m in cold:
        _, text, _ = edit.read_message(f, m.offset)
        assert text


def test_index_incremental_on_append(tmp_path, monkeypatch):
    monkeypatch.setattr(msgindex, "_DIR", tmp_path / "cache")
    f = tmp_path / "s.jsonl"
    _w(f, _rows())
    assert [m.uuid for m in msgindex.list_views(f)] == ["u1", "a1"]
    with open(f, "a") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u2", "parentUuid": "a1",
                             "message": {"role": "user", "content": "second"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "uuid": "a2", "parentUuid": "u2",
                             "message": {"role": "assistant",
                                         "content": [{"type": "text", "text": "reply two"}]}}) + "\n")
    grown = msgindex.list_views(f)
    assert [m.uuid for m in grown] == ["u1", "a1", "u2", "a2"]   # extended, not stale
    assert [m.uuid for m in grown] == [m.uuid for m in edit.list_messages(f)]


def test_index_invalidate_rebuilds(tmp_path, monkeypatch):
    monkeypatch.setattr(msgindex, "_DIR", tmp_path / "cache")
    f = tmp_path / "s.jsonl"
    _w(f, _rows())
    msgindex.list_views(f)
    edit.edit_message_text(f, "a1", "EDITED", backup=False)  # in-place change
    msgindex.invalidate(f)
    views = msgindex.list_views(f)
    _, text, _ = edit.read_message(f, next(m for m in views if m.uuid == "a1").offset)
    assert text == "EDITED"
