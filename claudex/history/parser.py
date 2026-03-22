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
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000)
        except Exception:
            return None
    if isinstance(value, str):
        for fmt in [
            lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
            lambda s: datetime.fromisoformat(s),
        ]:
            try:
                return fmt(value)
            except Exception:
                continue
    return None


def _extract_text(content) -> str:
    """Extract text from various Claude content formats."""
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if t == "text":
                    return block.get("text", "")[:200]
    if isinstance(content, dict):
        return content.get("text", str(content))[:200]
    return str(content)[:200]


def _parse_usage(data: dict) -> TokenUsage:
    usage = data.get("usage") or data.get("token_usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read=int(usage.get("cache_read_input_tokens", 0)),
        cache_write=int(usage.get("cache_creation_input_tokens", 0)),
    )


def parse_session_file(path: Path, profile_name: str) -> Optional[Session]:
    """
    Parse a Claude Code JSONL session file into a Session object.
    Defensive: never crashes on malformed input.
    """
    session_id = path.stem
    project_path = decode_project_path(path.parent.name)

    total_tokens = TokenUsage()
    message_count = 0
    title = "(untitled)"
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
                except json.JSONDecodeError:
                    continue

                if not isinstance(obj, dict):
                    continue

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
    for encoded_project in sorted(projects_dir.iterdir()):
        if not encoded_project.is_dir():
            continue
        for jsonl_file in sorted(encoded_project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            session = parse_session_file(jsonl_file, profile_name)
            if session:
                yield session
