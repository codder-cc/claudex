"""Session manager — resume, list, and launch sessions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDE_CONFIG_DIR_ENV, CLAUDE_BIN
from claudex.core.auth import AuthManager
from claudex.exceptions import ClaudeNotFoundError, SessionNotFoundError
from claudex.history.browser import HistoryBrowser
from claudex.history.models import Session


class SessionManager:
    def __init__(self, auth_manager: AuthManager, history_browser: HistoryBrowser) -> None:
        self._auth = auth_manager
        self._history = history_browser

    def list_sessions(self, profile_name: str, limit: int = 20) -> list[Session]:
        return self._history.get_sessions_for_profile(profile_name, limit=limit)

    def get_last_session(self, profile_name: str) -> Optional[Session]:
        return self._history.get_last_session(profile_name)

    def resume(
        self,
        profile_name: str,
        config_dir: Path,
        session_id: Optional[str] = None,
        strategy: str = "env",
    ) -> None:
        """
        Launch claude to resume a session.

        strategy:
          'env'    — just set CLAUDE_CONFIG_DIR and launch claude (claude picks up last session)
          'direct' — use --resume <session_id>
          'continue' — use --continue if available
        """
        if not self._claude_available():
            raise ClaudeNotFoundError()

        env = {**os.environ, **self._auth.get_env_for_profile(profile_name, config_dir)}

        if strategy == "direct":
            if not session_id:
                last = self._history.get_last_session(profile_name)
                if not last:
                    raise SessionNotFoundError()
                session_id = last.session_id
            cmd = [CLAUDE_BIN, "--resume", session_id]
        elif strategy == "continue":
            cmd = [CLAUDE_BIN, "--continue"]
        else:
            cmd = [CLAUDE_BIN]

        self._launch(cmd, env)

    def launch(self, profile_name: str, config_dir: Path, extra_args: list[str] | None = None) -> None:
        """Launch claude with a profile (fresh session)."""
        if not self._claude_available():
            raise ClaudeNotFoundError()
        env = {**os.environ, **self._auth.get_env_for_profile(profile_name, config_dir)}
        cmd = [CLAUDE_BIN] + (extra_args or [])
        self._launch(cmd, env)

    def migrate(self, session: Session, to_profile_name: str, to_config_dir: Path) -> Session:
        return self._history.migrate_session(session, to_profile_name, to_config_dir)

    def _launch(self, cmd: list[str], env: dict) -> None:
        if sys.platform != "win32":
            os.execvpe(cmd[0], cmd, env)  # Replace current process (Unix)
        else:
            subprocess.run(cmd, env=env)

    def _claude_available(self) -> bool:
        import shutil
        return shutil.which(CLAUDE_BIN) is not None
