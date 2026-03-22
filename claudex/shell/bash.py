"""Bash/Zsh shell integration generator."""

from __future__ import annotations

from pathlib import Path

from claudex.constants import CLAUDE_CONFIG_DIR_ENV, CLAUDEX_HOME, CURRENT_ENV_BASH
from claudex.shell.base import ShellIntegration


class BashIntegration(ShellIntegration):
    def __init__(self, shell: str = "bash") -> None:
        self.shell = shell  # "bash", "zsh", or "fish"

    def get_init_files(self) -> list[Path]:
        home = Path.home()
        if self.shell == "zsh":
            return [home / ".zshrc"]
        if self.shell == "fish":
            return [home / ".config" / "fish" / "config.fish"]
        return [home / ".bashrc", home / ".bash_profile"]

    def generate_env_file(self, config_dir: Path) -> str:
        return f'export {CLAUDE_CONFIG_DIR_ENV}="{config_dir}"\n'

    def generate_switch_function(self) -> str:
        env_file = CURRENT_ENV_BASH
        return f"""
claudex-switch() {{
    local profile_name="$1"
    if [ -z "$profile_name" ]; then
        echo "Usage: claudex-switch <profile-name>"
        return 1
    fi
    claudex _internal write-env "$profile_name"
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        source "{env_file}"
        export CLAUDEX_ACTIVE_PROFILE="$profile_name"
        echo "Switched to Claude profile: $profile_name"
        echo "  CLAUDE_CONFIG_DIR=${{CLAUDE_CONFIG_DIR}}"
    else
        echo "Profile '$profile_name' not found. Run 'claudex list' to see available profiles."
        return 1
    fi
}}"""

    def generate_profile_alias(self, profile_name: str, config_dir: Path) -> str:
        return f"""
claude-{profile_name}() {{
    {CLAUDE_CONFIG_DIR_ENV}="{config_dir.as_posix()}" claude "$@"
}}"""

    def generate_chpwd_hook(self) -> str:
        """Generate the .claudeprofile auto-switch hook."""
        env_file = CURRENT_ENV_BASH
        hook = f"""
_claudex_chpwd_hook() {{
    if [ -f ".claudeprofile" ]; then
        local profile
        profile=$(cat .claudeprofile | tr -d '[:space:]')
        if [ -n "$profile" ] && [ "$profile" != "$CLAUDEX_ACTIVE_PROFILE" ]; then
            claudex _internal write-env "$profile" 2>/dev/null
            if [ $? -eq 0 ]; then
                source "{env_file}"
                export CLAUDEX_ACTIVE_PROFILE="$profile"
                echo "[claudex] Auto-switched to profile: $profile"
            fi
        fi
    fi
}}"""
        if self.shell == "zsh":
            hook += """
autoload -U add-zsh-hook
add-zsh-hook chpwd _claudex_chpwd_hook
_claudex_chpwd_hook  # run on shell startup"""
        elif self.shell == "fish":
            return ""  # fish has different hook mechanism
        else:
            hook += """
# Run hook on directory change via PROMPT_COMMAND
if [[ -z "$PROMPT_COMMAND" ]]; then
    PROMPT_COMMAND="_claudex_chpwd_hook"
else
    PROMPT_COMMAND="_claudex_chpwd_hook;${PROMPT_COMMAND}"
fi
_claudex_chpwd_hook  # run on shell startup"""
        return hook

    def generate_init_script(self, profiles: list) -> str:
        parts = [
            f'# claudex {len(profiles)} profile(s) — auto-generated, edit with: claudex shell setup',
            f'export CLAUDEX_HOME="{CLAUDEX_HOME}"',
            '',
            self.generate_switch_function(),
            '',
        ]
        for profile in profiles:
            parts.append(self.generate_profile_alias(profile.name, profile.config_dir))
        parts.append('')
        parts.append(self.generate_chpwd_hook())
        return "\n".join(parts)
