"""Unit tests for shell integration generators."""

import pytest
from pathlib import Path

from claudex.shell.bash import BashIntegration
from claudex.shell.powershell import PowerShellIntegration
from claudex.core.profile import Profile
from datetime import datetime


def make_profile(name, config_dir):
    return Profile(
        name=name,
        config_dir=Path(config_dir),
        created_at=datetime.now(),
        aliases=[f"claude-{name}"],
    )


def test_bash_switch_function():
    b = BashIntegration("bash")
    fn = b.generate_switch_function()
    assert "claudex-switch" in fn
    assert "CLAUDE_CONFIG_DIR" in fn
    assert "source" in fn


def test_bash_profile_alias():
    b = BashIntegration("bash")
    alias = b.generate_profile_alias("work", Path("/home/user/.claudex/profiles/work"))
    assert "claude-work" in alias
    assert "CLAUDE_CONFIG_DIR" in alias
    assert "/home/user/.claudex/profiles/work" in alias.replace("\\", "/")


def test_bash_init_script_contains_profiles():
    b = BashIntegration("bash")
    profiles = [
        make_profile("work", "/home/user/.claudex/profiles/work"),
        make_profile("personal", "/home/user/.claudex/profiles/personal"),
    ]
    script = b.generate_init_script(profiles)
    assert "claude-work" in script
    assert "claude-personal" in script
    assert "claudex-switch" in script
    assert "_claudex_chpwd_hook" in script


def test_powershell_switch_function():
    ps = PowerShellIntegration()
    fn = ps.generate_switch_function()
    assert "Switch-ClaudeProfile" in fn
    assert "CLAUDE_CONFIG_DIR" in fn
    assert "claudex-switch" in fn


def test_powershell_profile_function():
    ps = PowerShellIntegration()
    fn = ps.generate_profile_alias("work", Path("C:/Users/user/.claudex/profiles/work"))
    assert "claude-work" in fn
    assert "CLAUDE_CONFIG_DIR" in fn


def test_bash_install(tmp_path):
    import claudex.constants as c
    b = BashIntegration("bash")
    profiles = [make_profile("work", str(tmp_path / "profiles" / "work"))]
    rc_file = tmp_path / ".bashrc"
    rc_file.write_text("# existing content\n")
    result = b.install(profiles, rc_file)
    content = rc_file.read_text()
    assert c.SHELL_MARKER_BEGIN in content
    assert c.SHELL_MARKER_END in content
    assert "claudex-switch" in content


def test_bash_install_idempotent(tmp_path):
    b = BashIntegration("bash")
    profiles = [make_profile("work", str(tmp_path / "profiles" / "work"))]
    rc_file = tmp_path / ".bashrc"
    rc_file.write_text("")
    b.install(profiles, rc_file)
    b.install(profiles, rc_file)  # second install
    content = rc_file.read_text()
    # Should only have one block
    from claudex.constants import SHELL_MARKER_BEGIN
    assert content.count(SHELL_MARKER_BEGIN) == 1
