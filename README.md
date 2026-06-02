# claudex

**Cross-platform Claude Code profile manager and session switcher**

Manage multiple Claude Code accounts with full session history, auth management, and a beautiful terminal UI. Works on Windows, macOS, and Linux.

---

## Features

- **Profile isolation** — each profile gets its own `CLAUDE_CONFIG_DIR` (separate history, settings, auth)
- **TUI dashboard** — interactive terminal UI with profiles, history browser, auth manager, settings
- **Auth management** — OAuth login, API key storage, token refresh, token expiry tracking (via system keychain on Windows/macOS/Linux)
- **Session browser** — browse, search, and resume conversations across all profiles
- **Session migration** — move a conversation from one profile to another
- **Cross-profile session resume** — resume a session from any profile without switching
- **Shell integration** — auto-generated `claude-work`, `claude-personal` functions + `claudex-switch`
- **Auto-switch** — place a `.claudeprofile` file in a project dir to auto-switch on `cd`
- **Encrypted cross-machine sharing** — push a profile (config + credentials, no history) to a remote server as an AES-256-GCM encrypted bundle; pull it on any other machine with a share token
- **MCP sharing** — register your sharing endpoint as an MCP server so Claude Code sessions can `share_profile` / `pull_profile` as native tools
- **Configurable sharing endpoint** — point `claudex` at any compatible server; no URL is hardcoded
- **Cross-platform** — Windows (PowerShell + Credential Manager), macOS (Keychain), Linux (Secret Service / file-based)
- **Self-update** — `claudex update` fast-forwards the local checkout (if pipx was installed from a path) and runs `pipx install --force`

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
claudex update [--ref REF]      Pull latest source and reinstall via pipx

claudex config set <key> <val>  Set a global config value
claudex config get <key>        Get a global config value

claudex share auth              Log in to the sharing server
claudex share push <name>       Encrypt and upload a profile; prints share token
claudex share pull <token> <n>  Download and decrypt a profile to a new name
claudex share list              List your uploaded shares
claudex share revoke <id>       Revoke an uploaded share

claudex fleet dispatch "<p>"    Run a headless `claude -p` job (auto-picks a profile)
claudex fleet status [job]      Show one job or a table of all
claudex fleet result <job>      Print a job's result
claudex fleet logs <job> -f     Tail a job's output log
claudex fleet fanout ...        Fan one task into parallel jobs across profiles
claudex fleet cancel <job>      Cancel a queued/running job
claudex fleet tick              Advance the queue once (cron-friendly)

claudex mcp setup <name>        Register the sharing MCP server for a profile
claudex mcp setup <name> --fleet  Register the local fleet MCP server (stdio)
claudex mcp serve               Run the local fleet MCP server (launched by Claude)
```

---

## Fleet — run agents across multiple subscriptions

Each profile is a separate Claude subscription. The **fleet** treats them as a
worker pool: it runs headless `claude -p` jobs in detached background processes,
routes each job to a profile that isn't rate-limited (auto-refreshing expired
OAuth tokens), and spreads load so one account's usage limit doesn't block you.
State lives under `~/.claudex/fleet/` — there is no daemon; the queue is advanced
by `claudex fleet tick` (called automatically by every fleet command).

```bash
# Fire off a long task — the scheduler picks the least-loaded eligible profile
claudex fleet dispatch "refactor the auth module and write tests"

# Pin to a specific subscription
claudex fleet dispatch "summarize docs/" --profile work --wait

# Fan one task out into parallel sub-agents across your subscriptions
claudex fleet fanout --task "port the test suite" \
  --subtask "convert tests/unit to pytest" \
  --subtask "convert tests/integration to pytest" \
  --subtask "update CI config"

claudex fleet status            # watch progress
claudex fleet result <job_id>   # read the answer
```

### Letting Claude drive the fleet (MCP)

Install the optional extra and register the local MCP server in a profile:

```bash
pipx install 'claudex[fleet]'        # adds the `mcp` SDK
claudex mcp setup work --fleet       # writes a stdio server into the profile
claudex use work                     # start Claude with that profile
```

Inside that Claude session, the tools `fleet_dispatch`, `fleet_status`,
`fleet_result`, `fleet_fanout`, `fleet_cancel`, and `fleet_list_profiles` become
available — so Claude can spawn agents onto your *other* subscriptions and gather
their results, all from one conversation.

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

## Cross-machine profile sharing

Share a profile (config, MCP servers, CLAUDE.md, credentials — but **not** session history) between machines using AES-256-GCM encryption. The server stores only ciphertext; the decryption key is embedded in the share token and never leaves the client.

**Step 1 — Configure your sharing server:**
```bash
claudex config set sharing.endpoint https://yourserver.com
```

**Step 2 — Authenticate and push (Computer 1):**
```bash
claudex share auth                          # log in once; JWT stored in keychain
claudex share push work --label "laptop"    # prints: cx_AbCdEf...  (save this token)
```

**Step 3 — Pull on another machine (Computer 2):**
```bash
claudex share auth                          # log in on this machine too
claudex share pull cx_AbCdEf... work-copy   # decrypts and reconstructs profile
claudex use work-copy                       # launch immediately — credentials restored
```

**MCP method (inside a Claude Code session):**
```bash
claudex mcp setup work                      # writes mcp_servers.json for the profile
# Then in a Claude Code session with that profile:
# → call share_profile tool to push
# → call pull_profile tool to pull
```

The encryption design: a 32-byte AES key is generated locally, the tar.gz bundle is encrypted, ciphertext is uploaded, and the share token encodes `base64url(uuid_bytes + aes_key)`. The server cannot decrypt stored bundles.

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
├── fleet/                    # fleet job state (no daemon)
│   ├── jobs/<id>.json        # one record per dispatched job
│   ├── logs/<id>.log         # combined stdout/stderr of each `claude -p`
│   ├── results/<id>.json     # parsed result per job
│   └── cooldowns.json        # per-profile rate-limit cooldowns
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
