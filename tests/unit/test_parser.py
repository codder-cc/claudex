"""Unit tests for JSONL session parser."""

import json
import pytest
from pathlib import Path
from datetime import datetime

from claudex.history.parser import parse_session_file, decode_project_path


@pytest.fixture
def session_file(tmp_path):
    project_dir = tmp_path / "projects" / "-home-user-myproject"
    project_dir.mkdir(parents=True)
    session_id = "3f8a2c1d-cafe-4242-beef-000000000001"
    path = project_dir / f"{session_id}.jsonl"

    lines = [
        {"role": "user", "content": "Fix the authentication bug", "timestamp": "2026-03-20T10:00:00"},
        {"role": "assistant", "content": "I'll help you fix the auth bug.",
         "usage": {"input_tokens": 150, "output_tokens": 200}, "timestamp": "2026-03-20T10:00:05"},
        {"role": "user", "content": "What's the issue?", "timestamp": "2026-03-20T10:01:00"},
        {"role": "assistant", "content": "The issue is in middleware.py",
         "usage": {"input_tokens": 50, "output_tokens": 80}, "timestamp": "2026-03-20T10:01:10"},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines))
    return path


def test_parse_basic(session_file):
    s = parse_session_file(session_file, "work")
    assert s is not None
    assert s.session_id == "3f8a2c1d-cafe-4242-beef-000000000001"
    assert s.profile_name == "work"
    assert s.title == "Fix the authentication bug"
    assert s.message_count == 4
    assert s.total_tokens.input_tokens == 200
    assert s.total_tokens.output_tokens == 280


def test_parse_empty_file(tmp_path):
    path = tmp_path / "project" / "empty.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    s = parse_session_file(path, "test")
    assert s is None


def test_parse_malformed_lines(tmp_path):
    path = tmp_path / "project" / "malformed.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n{broken\n" + json.dumps({"role": "user", "content": "Hello"}))
    s = parse_session_file(path, "test")
    assert s is not None
    assert s.title == "Hello"


def test_decode_project_path_url():
    result = decode_project_path("%2Fhome%2Fuser%2Fdev%2Fmyapp")
    assert result.as_posix() == "/home/user/dev/myapp"


def test_decode_project_path_dash():
    result = decode_project_path("-home-user-dev-myapp")
    assert "home" in str(result)
