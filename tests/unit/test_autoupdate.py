"""Tests for the global auto-update-disable launch default."""

from __future__ import annotations

from claudex.core import config


def test_default_config_disables_autoupdate():
    assert config._DEFAULT_CONFIG["disable_autoupdate"] is True


def test_global_launch_env_disables_by_default(monkeypatch):
    class _C:
        disable_autoupdate = True
    monkeypatch.setattr(config, "load_config", lambda: _C())
    assert config.global_launch_env() == {"DISABLE_AUTOUPDATER": "1"}


def test_global_launch_env_respects_enabled(monkeypatch):
    class _C:
        disable_autoupdate = False
    monkeypatch.setattr(config, "load_config", lambda: _C())
    assert config.global_launch_env() == {}


def test_config_property_roundtrip():
    cfg = config.GlobalConfig()
    cfg._data = dict(config._DEFAULT_CONFIG)
    assert cfg.disable_autoupdate is True
    cfg.disable_autoupdate = False
    assert cfg.disable_autoupdate is False
