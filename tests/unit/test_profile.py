"""Unit tests for ProfileManager."""

import pytest
from pathlib import Path
from datetime import datetime

from claudex.core.profile import Profile, ProfileManager
from claudex.exceptions import ProfileNotFoundError, ProfileExistsError


@pytest.fixture
def pm(tmp_path, monkeypatch):
    monkeypatch.setattr("claudex.constants.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr("claudex.constants.SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr("claudex.constants.CLAUDEX_HOME", tmp_path)
    monkeypatch.setattr("claudex.constants.ACTIVE_PROFILE_FILE", tmp_path / ".active_profile")
    monkeypatch.setattr("claudex.constants.CURRENT_ENV_BASH", tmp_path / ".current_env")
    monkeypatch.setattr("claudex.constants.CURRENT_ENV_PWSH", tmp_path / ".current_env.ps1")
    # Re-import to pick up patched constants
    import importlib
    import claudex.core.profile as mod
    importlib.reload(mod)
    from claudex.core.profile import ProfileManager as PM
    return PM()


def test_create_and_get(pm, tmp_path):
    p = pm.create("work", email="me@work.com")
    assert p.name == "work"
    assert p.email == "me@work.com"
    assert (tmp_path / "profiles" / "work" / "profile.toml").exists()


def test_create_duplicate_raises(pm):
    pm.create("work")
    with pytest.raises(ProfileExistsError):
        pm.create("work")


def test_get_missing_raises(pm):
    with pytest.raises(ProfileNotFoundError):
        pm.get("ghost")


def test_list(pm):
    pm.create("a")
    pm.create("b")
    pm.create("c")
    profiles = pm.list()
    assert len(profiles) == 3
    assert {p.name for p in profiles} == {"a", "b", "c"}


def test_delete(pm):
    pm.create("temp")
    pm.delete("temp")
    with pytest.raises(ProfileNotFoundError):
        pm.get("temp")


def test_aliases_default(pm):
    p = pm.create("myprofile")
    assert "claude-myprofile" in p.aliases


def test_set_active_writes_env(pm, tmp_path):
    pm.create("work")
    pm.set_active("work")
    env_file = tmp_path / ".current_env"
    assert env_file.exists()
    content = env_file.read_text()
    assert "CLAUDE_CONFIG_DIR" in content
    assert "work" in content
