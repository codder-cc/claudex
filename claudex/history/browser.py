"""History browser — aggregate, search, migrate sessions across profiles."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator, Optional

from claudex.core.switch import rewrite_transcript
from claudex.history.models import Session
from claudex.history.parser import iter_sessions


class HistoryBrowser:
    def __init__(self, profiles: list) -> None:
        """
        profiles: list of Profile objects (from ProfileManager).
        """
        self._profiles = profiles

    def get_all_sessions(
        self,
        profile_filter: Optional[str] = None,
        limit: int = 0,
    ) -> list[Session]:
        sessions: list[Session] = []
        for profile in self._profiles:
            if profile_filter and profile.name != profile_filter:
                continue
            for s in iter_sessions(profile.config_dir, profile.name):
                sessions.append(s)
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        if limit:
            return sessions[:limit]
        return sessions

    def get_sessions_for_profile(self, profile_name: str, limit: int = 50) -> list[Session]:
        return self.get_all_sessions(profile_filter=profile_name, limit=limit)

    def search(
        self,
        query: str,
        profile_filter: Optional[str] = None,
    ) -> list[Session]:
        # Parse the corpus once — transcripts can be hundreds of MB, so re-listing
        # per match strategy is wasteful and was a real performance bug.
        all_sessions = self.get_all_sessions(profile_filter=profile_filter)
        query_lower = query.lower()
        results: list[Session] = []
        seen: set[tuple[str, str]] = set()

        def _add(s: Session) -> None:
            key = (s.profile_name, s.session_id)
            if key not in seen:
                seen.add(key)
                results.append(s)

        for s in all_sessions:
            if (
                query_lower in s.title.lower()
                or query_lower in str(s.project_path).lower()
                or query_lower in s.session_id.lower()
            ):
                _add(s)

        # Optionally augment with rapidfuzz fuzzy title matching.
        try:
            from rapidfuzz import fuzz, process
            titles = [s.title for s in all_sessions]
            fuzzy_matches = process.extract(query, titles, scorer=fuzz.partial_ratio, limit=20)
            for _title, score, idx in fuzzy_matches:
                if score > 50:
                    _add(all_sessions[idx])
        except ImportError:
            pass
        return results

    def get_last_session(self, profile_name: str) -> Optional[Session]:
        sessions = self.get_sessions_for_profile(profile_name, limit=1)
        return sessions[0] if sessions else None

    def migrate_session(
        self,
        session: Session,
        to_profile_name: str,
        to_config_dir: Path,
        strip: bool = True,
    ) -> Session:
        """Copy a session from one profile to another, the way a switch carries it.

        The transcript goes through :func:`rewrite_transcript`, so signature
        blocks (thinking / redacted_thinking / connector_text) are stripped by
        default — they are bound to the issuing account and a different account
        rejects them with a 400 on the first request. The session's sibling
        directory (subagent transcripts, persisted tool results) comes along,
        its transcripts stripped the same way. ``strip=False`` copies verbatim,
        which is only safe between profiles of the same account.
        """
        if session.profile_name == to_profile_name:
            raise ValueError("Source and destination profiles are the same.")
        src_file = session.file_path
        dest_projects = to_config_dir / "projects" / src_file.parent.name
        dest_file = dest_projects / src_file.name
        if dest_file.resolve() == src_file.resolve():
            raise ValueError("Source and destination paths are identical.")
        if dest_file.exists():
            raise FileExistsError(
                f"A session '{session.session_id}' already exists in profile "
                f"'{to_profile_name}'."
            )
        rewrite_transcript(src_file, dest_file, strip=strip)
        src_dir = src_file.with_suffix("")
        if src_dir.is_dir():
            _carry_session_dir(src_dir, dest_file.with_suffix(""), strip)
        from claudex.history.parser import parse_session_file
        new_session = parse_session_file(dest_file, to_profile_name)
        if new_session is None:
            raise RuntimeError(f"Failed to parse migrated session at {dest_file}")
        return new_session

    def delete_session(self, session: Session) -> None:
        """Delete a session JSONL file."""
        session.file_path.unlink(missing_ok=True)


def _carry_session_dir(src: Path, dst: Path, strip: bool) -> None:
    """Copy a session's sibling directory, rewriting every transcript in it."""
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        if path.suffix == ".jsonl":
            rewrite_transcript(path, target, strip=strip)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
