"""Central constants for claudex."""

import sys
from pathlib import Path

# Claudex home directory
CLAUDEX_HOME = Path.home() / ".claudex"
PROFILES_DIR = CLAUDEX_HOME / "profiles"
SHARED_DIR = CLAUDEX_HOME / "shared"
GLOBAL_CONFIG_FILE = CLAUDEX_HOME / "config.toml"

# Runtime state files (written by Python, sourced by shell)
ACTIVE_PROFILE_FILE = CLAUDEX_HOME / ".active_profile"
CURRENT_ENV_BASH = CLAUDEX_HOME / ".current_env"       # bash/zsh: export VAR=value
CURRENT_ENV_PWSH = CLAUDEX_HOME / ".current_env.ps1"   # PowerShell: $env:VAR = value

# Claude Code env var
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

# Credential storage
CREDENTIAL_SERVICE = "claudex"

# Shell integration marker (used to detect existing installs)
SHELL_MARKER_BEGIN = "# >>> claudex shell integration >>>"
SHELL_MARKER_END   = "# <<< claudex shell integration <<<"

# Shared resources (can be symlinked across profiles)
SHAREABLE_RESOURCES = [
    "settings.json",
    "mcp_servers.json",
    "CLAUDE.md",
    "commands",
    "skills",
    "plugins",
]

# Platform detection
IS_WINDOWS = sys.platform == "win32"
IS_MACOS   = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")

# Claude binary name (platform-aware)
CLAUDE_BIN = "claude.exe" if IS_WINDOWS else "claude"
