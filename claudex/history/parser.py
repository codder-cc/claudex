"""JSONL session file parser for Claude Code transcripts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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


@dataclass
class _Meta:
    """Running metadata accumulated while scanning a transcript.

    Resumable: a fresh ``_Meta`` scans from the top; one rebuilt from a cache
    entry resumes from a byte offset and keeps accumulating (for a live session
    that only grew since last time)."""
    message_count: int = 0
    tokens: TokenUsage = field(default_factory=TokenUsage)
    title: str = "(untitled)"
    found_first_user: bool = False
    cwd: Optional[str] = None
    ai_title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_active: Optional[datetime] = None


def _fold_line(line: str, meta: _Meta) -> None:
    """Fold one JSONL line's metadata into *meta* (ignores junk)."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(obj, dict):
        return

    if meta.cwd is None and isinstance(obj.get("cwd"), str):
        meta.cwd = obj["cwd"]
    if meta.ai_title is None and isinstance(obj.get("aiTitle"), str):
        meta.ai_title = obj["aiTitle"]

    msg = obj
    if "message" in obj and isinstance(obj["message"], dict):
        msg = obj["message"]
    if "event" in obj:
        return

    role = msg.get("role", obj.get("type", ""))
    content = msg.get("content", "")
    ts = _parse_timestamp(
        msg.get("timestamp") or obj.get("timestamp") or
        msg.get("created_at") or obj.get("created_at")
    )
    if ts:
        if meta.started_at is None:
            meta.started_at = ts
        meta.last_active = ts

    if role in ("user", "human") and not meta.found_first_user:
        # Skip harness noise so the title is the first thing the human actually
        # typed — not a tool result, a meta "Caveat:" line, or slash-command
        # plumbing (<command-name>, <local-command-*>).
        title_text = _extract_text(content).strip()
        is_tool_result = (
            isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content)
        )
        if (not obj.get("isMeta") and not is_tool_result and title_text
                and not title_text.startswith(("<command-", "<local-command-"))):
            meta.found_first_user = True
            meta.title = title_text[:80]

    if role in ("assistant", "ai"):
        meta.tokens = meta.tokens + _parse_usage(obj if "usage" in obj else msg)
    if role:
        meta.message_count += 1


def _scan_into(path: Path, meta: _Meta, start_offset: int = 0,
               include_partial_tail: bool = True) -> int:
    """Fold a transcript's metadata into *meta*, starting at *start_offset*.

    Reads in binary so byte offsets are exact. The returned offset covers only
    complete (newline-terminated) lines, so an incremental re-scan never
    double-counts. A final line with no trailing newline is folded in when
    *include_partial_tail* is true (a one-shot parse of a file that simply isn't
    newline-terminated) but is left out of the offset and of the cached metadata
    when false (the live-session path, where that tail may still be mid-write).
    """
    consumed = start_offset
    try:
        with open(path, "rb") as f:
            f.seek(start_offset)
            for raw in f:
                terminated = raw.endswith(b"\n")
                line = raw.decode("utf-8", "replace").strip()
                if terminated:
                    consumed += len(raw)
                    if line:
                        _fold_line(line, meta)
                else:
                    if include_partial_tail and line:
                        _fold_line(line, meta)
                    break  # partial/final line — offset stays before it
    except (OSError, UnicodeDecodeError):
        pass
    return consumed


def _session_from_meta(path: Path, profile_name: str, meta: _Meta) -> Session:
    project_path = Path(meta.cwd) if meta.cwd else decode_project_path(path.parent.name)
    title = meta.ai_title[:80] if meta.ai_title else meta.title
    now = datetime.now()
    return Session(
        session_id=path.stem,
        project_path=project_path,
        profile_name=profile_name,
        file_path=path,
        started_at=meta.started_at or now,
        last_active=meta.last_active or now,
        message_count=meta.message_count,
        total_tokens=meta.tokens,
        title=title,
    )


def parse_session_file(path: Path, profile_name: str) -> Optional[Session]:
    """Parse a Claude Code JSONL session file into a Session. Never crashes."""
    meta = _Meta()
    _scan_into(path, meta, 0)
    if meta.message_count == 0:
        return None
    return _session_from_meta(path, profile_name, meta)


# ── cache (size/mtime keyed, incremental for growing sessions) ────────────────
def _meta_to_entry(meta: _Meta, size: int, mtime: int, offset: int) -> dict:
    from claudex.history.cache import iso
    t = meta.tokens
    return {
        "size": size, "mtime": mtime, "offset": offset,
        "message_count": meta.message_count,
        "tokens": [t.input_tokens, t.output_tokens, t.cache_read, t.cache_write],
        "title": meta.title, "found_first_user": meta.found_first_user,
        "cwd": meta.cwd, "ai_title": meta.ai_title,
        "started_at": iso(meta.started_at), "last_active": iso(meta.last_active),
    }


def _entry_to_meta(entry: dict) -> _Meta:
    from claudex.history.cache import from_iso
    tk = entry.get("tokens") or [0, 0, 0, 0]
    return _Meta(
        message_count=int(entry.get("message_count", 0)),
        tokens=TokenUsage(*(int(x) for x in (tk + [0, 0, 0, 0])[:4])),
        title=entry.get("title", "(untitled)"),
        found_first_user=bool(entry.get("found_first_user")),
        cwd=entry.get("cwd"), ai_title=entry.get("ai_title"),
        started_at=from_iso(entry.get("started_at")),
        last_active=from_iso(entry.get("last_active")),
    )


def iter_sessions(config_dir: Path, profile_name: str) -> Iterator[Session]:
    """Yield all sessions from a profile, using a size/mtime-keyed metadata cache.

    Unchanged files are served from the cache; a file that only grew is scanned
    incrementally from where we stopped last time; everything else is parsed in
    full. The cache is rewritten once at the end if anything changed.
    """
    from claudex.history.cache import load_cache, save_cache

    projects_dir = config_dir / "projects"
    if not projects_dir.exists():
        return

    cache = load_cache(config_dir)
    updated: dict = {}
    changed = False

    files: list = []
    for proj in projects_dir.iterdir():
        if proj.is_dir():
            files.extend(proj.glob("*.jsonl"))

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    for jf in sorted(files, key=_mtime, reverse=True):
        key = str(jf)
        try:
            st = jf.stat()
        except OSError:
            continue
        ent = cache.get(key)
        try:
            if ent and ent.get("size") == st.st_size and int(ent.get("mtime", -1)) == int(st.st_mtime):
                # Unchanged → serve from cache.
                updated[key] = ent
                meta = _entry_to_meta(ent)
                if meta.message_count == 0:
                    continue
                yield _session_from_meta(jf, profile_name, meta)
                continue
            if (ent and ent.get("offset") is not None
                    and ent.get("size", 1 << 62) <= st.st_size):
                # Grew (or unchanged tail) → resume from the cached offset.
                meta = _entry_to_meta(ent)
                off = _scan_into(jf, meta, int(ent["offset"]), include_partial_tail=False)
            else:
                # New / shrunk / rotated → full parse (complete lines only, so the
                # offset + counts we cache stay consistent for later incrementals).
                meta = _Meta()
                off = _scan_into(jf, meta, 0, include_partial_tail=False)
            updated[key] = _meta_to_entry(meta, st.st_size, int(st.st_mtime), off)
            changed = True
            if meta.message_count == 0:
                continue
            yield _session_from_meta(jf, profile_name, meta)
        except Exception:
            # One unparseable transcript must never abort the whole listing.
            continue

    if changed or set(updated) != set(cache):
        save_cache(config_dir, updated)
