# claudex

**Cross-platform Claude Code profile manager and session switcher**

Manage multiple Claude Code accounts with full session history, auth management, and a beautiful terminal UI. Works on Windows, macOS, and Linux.

---

## Features

- **Profile isolation** — each profile gets its own `CLAUDE_CONFIG_DIR` (separate history, settings, auth)
- **TUI dashboard** — interactive terminal UI with profiles, history browser, auth manager, settings
- **Auth management** — OAuth login, API key storage, token expiry tracking (via system keychain on Windows/macOS)
- **Session browser** — browse, search, and resume conversations across all profiles
- **Session migration** — move a conversation from one profile to another
- **Shell integration** — auto-generated `claude-work`, `claude-personal` functions + `claudex-switch`
- **Auto-switch** — place a `.claudeprofile` file in a project dir to auto-switch on `cd`
- **Cross-platform** — Windows (PowerShell + Credential Manager), macOS (Keychain), Linux (file-based)

---

## Install

**One-liner (macOS / Linux):**

```bash
curl -fsSL https://raw.githubusercontent.com/codder-cc/claudex/main/install.sh | sh
```

Installs via pipx (or falls back to pip), then sets up shell integration automatically.

**Manual:**

```bash
pip install claudex
# or
pipx install claudex
```

**Windows:**

```powershell
pip install claudex
```

---

## Quick Start

```bash
# Create profiles
claudex new work --email me@company.com
claudex new personal --email me@gmail.com

# Authenticate
claudex auth add work        # triggers claude /login with profile's CLAUDE_CONFIG_DIR
claudex auth add personal

# See all profiles
claudex list

# Install shell integration (bash/zsh/PowerShell auto-detected)
claudex shell setup

# Restart shell, then switch profiles
claudex-switch work          # sets CLAUDE_CONFIG_DIR in current shell
claude                       # launches with work profile

# Or one-shot (no persistent switch)
claudex use work             # launches claude, then returns to previous env

# Per-profile aliases
claude-work                  # always uses work profile
claude-personal              # always uses personal profile

# Launch TUI dashboard
claudex
```

---

## Commands

```
claudex                         Launch TUI dashboard
claudex list                    List all profiles
claudex new <name>              Create a new profile
claudex switch <name>           Set active profile
claudex use <name> [args...]    One-shot: launch claude with profile
claudex delete <name>           Delete a profile
claudex rename <old> <new>      Rename a profile
claudex export <name>           Export profile to .tar.gz
claudex import <file>           Import profile from .tar.gz

claudex auth add <name>         OAuth login for profile
claudex auth key <name>         Add API key for profile
claudex auth status             Auth status for all profiles
claudex auth revoke <name>      Clear stored credentials

claudex session list [name]     List sessions
claudex session resume [name]   Resume last session
claudex session migrate <id>    Move session between profiles

claudex history                 Open history browser (TUI)
claudex search <query>          Search sessions

claudex shell setup             Install shell integration
claudex shell hook              Print shell snippet

claudex doctor                  Diagnose installation issues
```

---

## TUI Screens

| Key | Screen |
|-----|--------|
| `1` | Profile dashboard |
| `2` | Session history browser |
| `3` | Auth manager |
| `4` | Settings |
| `?` | Help |
| `q` | Quit |

**Profile dashboard actions:** `n` new, `d` delete, `Enter` switch, `l` launch, `a` auth

**History browser actions:** `Enter` resume, `m` migrate, `x` delete, `/` search

---

## Auto-switch on directory change

Place a `.claudeprofile` file in any project directory:
```
echo "work" > ~/projects/company-app/.claudeprofile
```

When you `cd` into that directory, your shell will auto-switch to the `work` profile.

---

## How it works

Each profile is a directory at `~/.claudex/profiles/<name>/` which is used as `CLAUDE_CONFIG_DIR`. This gives complete isolation of:

- Conversation history (`projects/`)
- Settings (`settings.json`)
- MCP server configs
- Auth tokens (`.credentials.json` on Linux, system keychain on Windows/macOS)
- Per-profile memory (`CLAUDE.md`)

The shell integration generates functions that set `CLAUDE_CONFIG_DIR` in the parent shell. The key insight: a Python subprocess **cannot** modify the parent shell's environment — so `claudex switch` writes a file that your shell **sources**.

---

## Storage layout

```
~/.claudex/
├── config.toml               # global settings
├── .active_profile            # currently active profile name
├── .current_env              # sourced by bash/zsh after switch
├── .current_env.ps1          # sourced by PowerShell after switch
├── shared/                   # shared resources (symlinked into profiles)
│   ├── CLAUDE.md
│   └── settings.json
└── profiles/
    ├── work/
    │   ├── profile.toml      # profile metadata
    │   ├── projects/         # Claude Code session history
    │   │   └── <encoded>/
    │   │       └── <uuid>.jsonl
    │   └── ...               # all other Claude Code state
    └── personal/
        ├── profile.toml
        └── ...
```

---

## Platform notes

| Platform | Credential storage | Shell |
|----------|--------------------|-------|
| Windows  | Windows Credential Manager | PowerShell |
| macOS    | Keychain | bash/zsh |
| Linux    | Secret Service (if available) else `~/.claudex/.credentials.json` | bash/zsh/fish |

---

## Development

```bash
git clone <repo>
cd claudex
pip install -e ".[dev]"
pytest tests/
```
