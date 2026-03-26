#!/usr/bin/env sh
# claudex installer
# Usage: curl -fsSL https://raw.githubusercontent.com/codder-cc/claudex/main/install.sh | sh

set -e

REPO="codder-cc/claudex"
PACKAGE="claudex"
MIN_PYTHON_MINOR=10   # requires 3.10+

# ── colours ────────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

info()    { printf "${BOLD}${GREEN}==>${RESET} %s\n" "$*"; }
warn()    { printf "${BOLD}${YELLOW}warn:${RESET} %s\n" "$*"; }
error()   { printf "${BOLD}${RED}error:${RESET} %s\n" "$*" >&2; exit 1; }

# ── helpers ────────────────────────────────────────────────────────────────────
have() { command -v "$1" >/dev/null 2>&1; }
# Like have(), but also verifies the command actually runs (guards against broken pyenv shims)
works() { "$1" --version >/dev/null 2>&1; }

find_python() {
  for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if have "$cmd"; then
      ver=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || true)
      maj=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || true)
      if [ "$maj" = "3" ] && [ "${ver:-0}" -ge "$MIN_PYTHON_MINOR" ] 2>/dev/null; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

detect_shell_rc() {
  case "${SHELL:-}" in
    */zsh)  echo "$HOME/.zshrc" ;;
    */bash)
      if [ -f "$HOME/.bash_profile" ]; then echo "$HOME/.bash_profile"
      else echo "$HOME/.bashrc"; fi ;;
    */fish) echo "$HOME/.config/fish/config.fish" ;;
    *)      echo "$HOME/.profile" ;;
  esac
}

# ── preflight ──────────────────────────────────────────────────────────────────
printf "\n${BOLD}claudex installer${RESET}\n\n"

OS=$(uname -s)
case "$OS" in
  Darwin) info "Platform: macOS" ;;
  Linux)  info "Platform: Linux" ;;
  *)      error "Unsupported platform: $OS. Windows users: use 'pip install claudex' in PowerShell." ;;
esac

# ── find Python ────────────────────────────────────────────────────────────────
info "Looking for Python 3.10+..."
PYTHON=$(find_python) || {
  error "Python 3.10+ not found.\n\n  macOS:  brew install python\n  Linux:  sudo apt install python3 / sudo dnf install python3"
}
PYTHON_VERSION=$("$PYTHON" --version 2>&1)
info "Using $PYTHON_VERSION ($PYTHON)"

# ── install via pipx (preferred) or pip ───────────────────────────────────────
PIPX_CMD=""
if have pipx && works pipx; then
  PIPX_CMD="pipx"
elif "$PYTHON" -m pipx --version >/dev/null 2>&1; then
  PIPX_CMD="$PYTHON -m pipx"
fi

if [ -n "$PIPX_CMD" ]; then
  info "Installing with pipx..."
  $PIPX_CMD install "$PACKAGE" 2>&1 || {
    warn "Already installed — upgrading..."
    $PIPX_CMD upgrade "$PACKAGE" 2>&1 || error "pipx install/upgrade failed."
  }
  $PIPX_CMD ensurepath >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"

else
  warn "pipx not found — installing pipx first..."

  if have brew && works brew; then
    info "Installing pipx via Homebrew..."
    brew install pipx >/dev/null 2>&1
    export PATH="$(brew --prefix)/bin:$PATH"
  else
    info "Installing pipx via pip..."
    "$PYTHON" -m pip install --quiet --user pipx
    export PATH="$HOME/.local/bin:$PATH"
  fi

  # re-resolve pipx after install
  if have pipx && works pipx; then PIPX_CMD="pipx"
  else PIPX_CMD="$PYTHON -m pipx"; fi

  $PIPX_CMD ensurepath >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"

  info "Installing claudex with pipx..."
  $PIPX_CMD install "$PACKAGE" 2>&1 || error "Installation failed."
fi

# ── verify ─────────────────────────────────────────────────────────────────────
if have claudex; then
  CLAUDEX_VERSION=$(claudex --version 2>/dev/null || echo "unknown")
  info "claudex ${CLAUDEX_VERSION} installed successfully."
else
  # pipx ensurepath may not have taken effect yet; try common locations
  for dir in "$HOME/.local/bin" "$HOME/.pipx/bin"; do
    if [ -x "$dir/claudex" ]; then
      export PATH="$dir:$PATH"
      info "claudex installed (added $dir to PATH for this session)."
      break
    fi
  done
fi

# ── shell integration ──────────────────────────────────────────────────────────
info "Setting up shell integration..."
claudex shell setup --no-restart 2>/dev/null || claudex shell setup 2>/dev/null || \
  warn "Could not auto-setup shell integration. Run 'claudex shell setup' manually."

# ── PATH reminder ─────────────────────────────────────────────────────────────
RC=$(detect_shell_rc)
printf "\n${BOLD}All done!${RESET}\n\n"
printf "  Reload your shell to apply PATH changes:\n"
printf "    ${BOLD}source %s${RESET}\n\n" "$RC"
printf "  Then get started:\n"
printf "    ${BOLD}claudex new work${RESET}          create a profile\n"
printf "    ${BOLD}claudex auth add work${RESET}     authenticate\n"
printf "    ${BOLD}claudex${RESET}                   open the TUI dashboard\n\n"
