"""Integration tests for CLI commands."""

import pytest
from click.testing import CliRunner
from pathlib import Path

from claudex.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Run each test with a clean ~/.claudex equivalent."""
    monkeypatch.setattr("claudex.constants.CLAUDEX_HOME", tmp_path)
    monkeypatch.setattr("claudex.constants.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr("claudex.constants.SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr("claudex.constants.GLOBAL_CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr("claudex.constants.ACTIVE_PROFILE_FILE", tmp_path / ".active_profile")
    monkeypatch.setattr("claudex.constants.CURRENT_ENV_BASH", tmp_path / ".current_env")
    monkeypatch.setattr("claudex.constants.CURRENT_ENV_PWSH", tmp_path / ".current_env.ps1")
    # Reload affected modules
    import importlib
    import claudex.core.profile
    import claudex.core.config
    importlib.reload(claudex.core.profile)
    importlib.reload(claudex.core.config)


def test_list_empty(runner):
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "No profiles" in result.output


def test_new_profile(runner):
    result = runner.invoke(cli, ["new", "work", "--email", "me@work.com"])
    assert result.exit_code == 0
    assert "work" in result.output


def test_new_duplicate(runner):
    runner.invoke(cli, ["new", "work"])
    result = runner.invoke(cli, ["new", "work"])
    assert result.exit_code != 0 or "already exists" in result.output


def test_list_after_create(runner):
    runner.invoke(cli, ["new", "work"])
    runner.invoke(cli, ["new", "personal"])
    result = runner.invoke(cli, ["list"])
    assert "work" in result.output
    assert "personal" in result.output


def test_switch_nonexistent(runner):
    result = runner.invoke(cli, ["switch", "ghost"])
    assert result.exit_code != 0


def test_switch_existing(runner, tmp_path):
    runner.invoke(cli, ["new", "work"])
    result = runner.invoke(cli, ["switch", "work"])
    assert result.exit_code == 0


def test_delete_profile(runner):
    runner.invoke(cli, ["new", "temp"])
    result = runner.invoke(cli, ["delete", "temp", "--yes"])
    assert result.exit_code == 0


def test_shell_setup_print_only(runner):
    runner.invoke(cli, ["new", "work"])
    result = runner.invoke(cli, ["shell", "setup", "--print-only"])
    assert result.exit_code == 0
    assert "claudex-switch" in result.output or "Switch-ClaudeProfile" in result.output


def test_session_list_empty(runner):
    runner.invoke(cli, ["new", "work"])
    result = runner.invoke(cli, ["session", "list", "work"])
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_auth_status_empty(runner):
    runner.invoke(cli, ["new", "work"])
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0
    assert "work" in result.output
