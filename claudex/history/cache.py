"""Per-profile cache of session-list metadata.

Listing history means reading title / message-count / token totals for every
transcript, and those totals need a full scan of each file. Claude Code
transcripts run to hundreds of MB, and the *active* one keeps growing, so
re-scanning them on every `claudex history` made the list crawl.

This cache stores the parsed metadata keyed by the file's ``(size, mtime)``.
An unchanged file is served from the cache instantly; a file that only *grew*
(the live session) is scanned **incrementally** from the byte offset we stopped
at last time, so we touch just the new tail. The cache is advisory — a miss or a
corrupt cache only costs a re-parse, never wrong behaviour.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

CACHE_NAME = ".claudex-session-cache.json"
_CACHE_VERSION = 1


def cache_path(config_dir: Path) -> Path:
    return Path(config_dir) / CACHE_NAME


def load_cache(config_dir: Path) -> dict:
    try:
        data = json.loads(cache_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("v") != _CACHE_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_cache(config_dir: Path, entries: dict) -> None:
    payload = {"v": _CACHE_VERSION, "entries": entries}
    p = cache_path(config_dir)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, p)
    except OSError:
        pass  # a cache we can't write is simply not cached


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def from_iso(s) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
