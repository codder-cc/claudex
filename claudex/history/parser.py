"""JSONL session file parser for Claude Code transcripts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import unquote

from claudex.history.models import Message, Session, TokenUsage


def decode_project_path(encoded: str) -> Path:
    """
    Decode Claude Code's project path encoding from directory names.
    Claude encodes '/' as '-' in path components (observed behaviour).
    Falls back to URL decode, then raw string.

    NOTE: this is inherently lossy — a directory whose real name contains a
    hyphen is indistinguishable from a path separator. Callers should prefer the
    ``cwd`` field embedded in the transcript when it is available; this is only a
    best-effort fallback for display.
    """
    # Common pattern: /home/user/dev/project → -home-user-dev-project
    # or URL encoded: %2Fhome%2Fuser%2Fdev%2Fproject
    if encoded.startswith("%2F") or "%2F" in encoded:
        decoded = unquote(encoded)
        return Path(decoded)
    # Heuristic: if starts with '-' on Unix paths, replace leading '-' with '/'
    if encoded.startswith("-"):
        decoded = encoded.replace("-", "/", 1).replace("-", "/")
        return Path(decoded)
    return Path(encoded)


def _parse_timestamp(value) -> Optional[datetime]:
    """Parse a timestamp into a *naive local* datetime.

    Claude transcripts mix epoch-millisecond ints, ISO strings with a ``Z``/offset
    (timezone-aware), and ISO strings with no zone. We normalise everything to a
    single naive-local representation so that sorting and ``datetime.now()`` deltas
    never raise "can't compare offset-naive and offset-aware datetimes".
    """
    if not value:
        return None
    dt: Optional[datetime] = None
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(value / 1000)  # naive local
        except (ValueError, OSError, OverflowError):
            return None
    elif isinstance(value, str):
        for fmt in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
                    datetime.fromisoformat):
            try:
                dt = fmt(value)
                break
            except (ValueError, TypeError):
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # → naive local
    return dt


def _extract_text(content) -> str:
    """Extract a short title string from various Claude content formats."""
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type", "") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    return text[:200]
    if isinstance(content, dict):
        text = content.get("text")
        return (text if isinstance(text, str) else str(content))[:200]
    return str(content)[:200]


def _safe_int(value) -> int:
    """Coerce a usage field to int, tolerating None / strings / junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_usage(data: dict) -> TokenUsage:
    usage = data.get("usage") or data.get("token_usage") or {}
    if not isinstance(usage, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_safe_int(usage.get("input_tokens", 0)),
        output_tokens=_safe_int(usage.get("output_tokens", 0)),
        cache_read=_safe_int(usage.get("cache_read_input_tokens", 0)),
        cache_write=_safe_int(usage.get("cache_creation_input_tokens", 0)),
    )


def parse_session_file(path: Path, profile_name: str) -> Optional[Session]:
    """
    Parse a Claude Code JSONL session file into a Session object.
    Defensive: never crashes on malformed input.
    """
    session_id = path.stem

    total_tokens = TokenUsage()
    message_count = 0
    title = "(untitled)"
    cwd: Optional[str] = None
    ai_title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_active: Optional[datetime] = None
    found_first_user = False

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if not isinstance(obj, dict):
                    continue

                # Prefer Claude's own recorded cwd / title over reconstructions.
                if cwd is None and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if ai_title is None and isinstance(obj.get("aiTitle"), str):
                    ai_title = obj["aiTitle"]

                # Handle both flat message and wrapped event formats
                msg = obj
                if "message" in obj and isinstance(obj["message"], dict):
                    msg = obj["message"]
                if "event" in obj:
                    continue  # skip non-message events

                role = msg.get("role", obj.get("type", ""))
                content = msg.get("content", "")
                ts = _parse_timestamp(
                    msg.get("timestamp") or obj.get("timestamp") or
                    msg.get("created_at") or obj.get("created_at")
                )

                if ts:
                    if started_at is None:
                        started_at = ts
                    last_active = ts

                if role in ("user", "human") and not found_first_user:
                    found_first_user = True
                    title_text = _extract_text(content).strip()
                    title = title_text[:80] if title_text else "(untitled)"

                if role in ("assistant", "ai"):
                    usage = _parse_usage(obj if "usage" in obj else msg)
                    total_tokens = total_tokens + usage

                if role:
                    message_count += 1

    except (OSError, UnicodeDecodeError):
        return None

    if message_count == 0:
        return None

    # cwd from the transcript is authoritative; decode_project_path is the lossy
    # fallback only when the field is absent. A Claude-generated aiTitle beats the
    # truncated first user message.
    project_path = Path(cwd) if cwd else decode_project_path(path.parent.name)
    if ai_title:
        title = ai_title[:80]

    now = datetime.now()
    return Session(
        session_id=session_id,
        project_path=project_path,
        profile_name=profile_name,
        file_path=path,
        started_at=started_at or now,
        last_active=last_active or now,
        message_count=message_count,
        total_tokens=total_tokens,
        title=title,
    )


def iter_sessions(config_dir: Path, profile_name: str) -> Iterator[Session]:
    """Yield all sessions from a profile's projects directory."""
    projects_dir = config_dir / "projects"
    if not projects_dir.exists():
        return
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    for encoded_project in sorted(projects_dir.iterdir()):
        if not encoded_project.is_dir():
            continue
        for jsonl_file in sorted(encoded_project.glob("*.jsonl"), key=_mtime, reverse=True):
            try:
                session = parse_session_file(jsonl_file, profile_name)
            except Exception:
                # One unparseable transcript must never abort the whole listing.
                continue
            if session:
                yield session
