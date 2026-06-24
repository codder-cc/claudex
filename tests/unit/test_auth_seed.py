"""Regression tests for auth-add identity seeding and keychain awareness."""

import json

from claudex.cli import _seed_claude_json
from claudex.core.auth import AuthManager


def _fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "home-uuid", "emailAddress": "home@x.com"},
        "userID": "home-user",
        "hasCompletedOnboarding": True,
        "installMethod": "pipx",
    }))
    monkeypatch.setattr("claudex.cli.Path.home", lambda: home)
    return home


def test_auth_add_does_not_seed_foreign_identity(tmp_path, monkeypatch):
    """Bug 2: `auth add` must not copy the home account's oauthAccount/userID into
    a profile that may belong to a different account."""
    _fake_home(tmp_path, monkeypatch)
    cfg = tmp_path / "profile"
    cfg.mkdir()

    _seed_claude_json(cfg, seed_identity=False)

    data = json.loads((cfg / ".claude.json").read_text())
    assert "oauthAccount" not in data
    assert "userID" not in data
    # Non-identity onboarding keys are still seeded (suppress noise, not auth).
    assert data.get("hasCompletedOnboarding") is True


def test_import_current_does_seed_identity(tmp_path, monkeypatch):
    """import-current imports the *current* (home) login → identity match is correct."""
    _fake_home(tmp_path, monkeypatch)
    cfg = tmp_path / "profile2"
    cfg.mkdir()

    _seed_claude_json(cfg, seed_identity=True)

    data = json.loads((cfg / ".claude.json").read_text())
    assert data["oauthAccount"]["accountUuid"] == "home-uuid"


def test_existing_oauthaccount_wins_over_seed(tmp_path, monkeypatch):
    """If Claude already wrote the correct oauthAccount, seeding must not clobber it."""
    _fake_home(tmp_path, monkeypatch)
    cfg = tmp_path / "profile3"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "real-account", "emailAddress": "real@x.com"},
    }))

    _seed_claude_json(cfg, seed_identity=True)

    data = json.loads((cfg / ".claude.json").read_text())
    assert data["oauthAccount"]["accountUuid"] == "real-account"


def test_macos_keychain_token_present_no_entry(tmp_path):
    """A profile with no Keychain entry reports False on macOS, None elsewhere."""
    from claudex.constants import IS_MACOS

    result = AuthManager().macos_keychain_token_present(tmp_path / "nonexistent-profile")
    if IS_MACOS:
        assert result is False
    else:
        assert result is None
