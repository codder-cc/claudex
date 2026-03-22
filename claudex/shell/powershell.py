"""PowerShell integration generator."""

from __future__ import annotations

from pathlib import Path

from claudex.constants import CLAUDE_CONFIG_DIR_ENV, CLAUDEX_HOME, CURRENT_ENV_PWSH
from claudex.shell.base import ShellIntegration


class PowerShellIntegration(ShellIntegration):
    def get_init_files(self) -> list[Path]:
        """Return PowerShell profile path(s)."""
        import subprocess
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "echo $PROFILE"],
                capture_output=True, text=True, timeout=5,
            )
            profile_path = result.stdout.strip()
            if profile_path:
                return [Path(profile_path)]
        except Exception:
            pass
        # Fallback
        home = Path.home()
        return [home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"]

    def generate_env_file(self, config_dir: Path) -> str:
        return f'$env:{CLAUDE_CONFIG_DIR_ENV} = "{config_dir}"\n'

    def generate_switch_function(self) -> str:
        env_file = CURRENT_ENV_PWSH
        return f"""
function Switch-ClaudeProfile {{
    param([Parameter(Mandatory=$true)][string]$ProfileName)
    claudex _internal write-env $ProfileName
    if ($LASTEXITCODE -eq 0) {{
        . "{env_file}"
        $env:CLAUDEX_ACTIVE_PROFILE = $ProfileName
        Write-Host "Switched to Claude profile: $ProfileName" -ForegroundColor Green
        Write-Host "  CLAUDE_CONFIG_DIR=$env:{CLAUDE_CONFIG_DIR_ENV}"
    }} else {{
        Write-Host "Profile '$ProfileName' not found. Run 'claudex list' to see profiles." -ForegroundColor Red
    }}
}}
Set-Alias claudex-switch Switch-ClaudeProfile"""

    def generate_profile_alias(self, profile_name: str, config_dir: Path) -> str:
        fn_name = f"claude-{profile_name}"
        return f"""
function {fn_name} {{
    $env:{CLAUDE_CONFIG_DIR_ENV} = "{config_dir}"
    claude @args
}}"""

    def generate_chpwd_hook(self) -> str:
        env_file = CURRENT_ENV_PWSH
        return f"""
function _ClaudexDirHook {{
    $profileFile = Join-Path (Get-Location) ".claudeprofile"
    if (Test-Path $profileFile) {{
        $profileName = (Get-Content $profileFile -Raw).Trim()
        if ($profileName -and $profileName -ne $env:CLAUDEX_ACTIVE_PROFILE) {{
            claudex _internal write-env $profileName 2>$null
            if ($LASTEXITCODE -eq 0) {{
                . "{env_file}"
                $env:CLAUDEX_ACTIVE_PROFILE = $profileName
                Write-Host "[claudex] Auto-switched to profile: $profileName"
            }}
        }}
    }}
}}
# Register directory change hook
$ExecutionContext.SessionState.InvokeCommand.LocationChangedAction = {{
    _ClaudexDirHook
}}
_ClaudexDirHook  # run on profile load"""

    def generate_init_script(self, profiles: list) -> str:
        parts = [
            f'# claudex {len(profiles)} profile(s) — auto-generated',
            f'$env:CLAUDEX_HOME = "{CLAUDEX_HOME}"',
            '',
            self.generate_switch_function(),
            '',
        ]
        for profile in profiles:
            parts.append(self.generate_profile_alias(profile.name, profile.config_dir))
        parts.append('')
        parts.append(self.generate_chpwd_hook())
        return "\n".join(parts)
