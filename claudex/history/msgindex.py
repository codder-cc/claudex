"""Cached, lazily-loaded message index for the transcript editor.

Opening the editor on a hundred-MB session meant a full scan every time (~5s).
This builds a compact per-message index — ``uuid, parent, role, kind, turn,
byte-offset, preview`` for each message on the active chain — and caches it keyed
by the file's ``(size, mtime)``. An unchanged file loads from the cache
instantly; the live session, which only grows, is extended incrementally by
scanning just the new tail and splicing it onto the cached chain. The editor
shows the list from these previews and reads a message's full text lazily, by
offset, only when it is selected or edited (see :func:`edit.read_message`).

The cache is advisory — a miss, a corrupt file or a chain that no longer splices
simply triggers a full rebuild.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

from claudex.constants import CLAUDEX_HOME
from claudex.history import edit

_DIR = CLAUDEX_HOME / ".msgindex"
_PREVIEW = 100
# rec layout: [uuid, parent, role, kind, turn(0/1), offset, preview]


def _cache_file(path: Path) -> Path:
    h = hashlib.sha1(str(Path(path).resolve()).encode("utf-8", "replace")).hexdigest()
    return _DIR / f"{h}.json"


def _rec(scan: edit._Scan, u: str) -> list:
    preview = " ".join(scan.text.get(u, "").split())[:_PREVIEW]
    return [u, scan.parent_of.get(u), scan.role[u], scan.kind[u],
            1 if u in scan.turn else 0, scan.offset.get(u, -1), preview]


def _build_full(path: Path):
    scan = edit._scan(path)
    chain = edit._chain_uuids(scan)
    return [_rec(scan, u) for u in chain], scan.leaf, scan.scanned


def _extend(path: Path, cached: dict):
    """Grow a cached chain by scanning only the appended tail. Returns the new
    (recs, leaf, scanned) or ``None`` if the tail doesn't splice cleanly."""
    scan = edit._scan(path, int(cached["scanned"]))
    if not scan.order:
        return cached["recs"], cached.get("leaf"), scan.scanned
    chain_set = {r[0] for r in cached["recs"]}
    seg: List[str] = []
    cur = scan.leaf
    seen: set = set()
    attach = None
    while cur and cur not in seen:
        seen.add(cur)
        if cur in chain_set:
            attach = cur
            break
        if cur in scan.role:
            seg.append(cur)
        nxt = scan.parent_of.get(cur)
        if nxt is None:
            break
        cur = nxt
    if attach is None:
        return None
    seg.reverse()
    recs: list = []
    for r in cached["recs"]:
        recs.append(r)
        if r[0] == attach:
            break
    recs.extend(_rec(scan, u) for u in seg)
    return recs, scan.leaf or cached.get("leaf"), scan.scanned


def _save(cf: Path, size: int, mtime: int, recs: list, leaf, scanned: int) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        tmp = cf.with_suffix(".tmp")
        tmp.write_text(json.dumps({"size": size, "mtime": mtime, "scanned": scanned,
                                   "leaf": leaf, "recs": recs}), encoding="utf-8")
        os.replace(tmp, cf)
    except OSError:
        pass


def load_recs(path: Path) -> list:
    """The cached chain index, rebuilt / extended as needed."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return []
    cf = _cache_file(path)
    cached = None
    try:
        data = json.loads(cf.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("recs"), list):
            cached = data
    except (OSError, ValueError):
        cached = None

    if cached and cached.get("size") == st.st_size and int(cached.get("mtime", -1)) == int(st.st_mtime):
        return cached["recs"]

    result = None
    if (cached and cached.get("scanned") is not None
            and int(cached.get("size", 1 << 62)) <= st.st_size):
        result = _extend(path, cached)
    if result is None:
        result = _build_full(path)
    recs, leaf, scanned = result
    _save(cf, st.st_size, int(st.st_mtime), recs, leaf, scanned)
    return recs


_HIDDEN = ("thinking", "tool_use", "tool_result", "command", "meta", "error")


def list_views(path: Path, *, turns: bool = False) -> List[edit.MsgView]:
    """Lightweight MsgViews (preview + offset) for the editor, from the cache.

    Text is the preview only; the editor loads a message's full content lazily
    with :func:`edit.read_message` using the ``offset`` field.
    """
    recs = load_recs(path)
    round_of: dict = {}
    r = -1
    for rec in recs:
        if rec[4]:
            r += 1
        round_of[rec[0]] = r if r >= 0 else 0
    views: List[edit.MsgView] = []
    for uuid, parent, role, kind, turn, offset, preview in recs:
        if turns:
            if role == "user" and not turn:
                continue
            if role == "assistant" and kind != "text":
                continue
            if kind in _HIDDEN:
                continue
        views.append(edit.MsgView(
            uuid=uuid, role=role, kind=kind, text=preview,
            round_index=round_of.get(uuid, -1),
            editable=(kind in edit._EDITABLE_KINDS),
            on_active_chain=True, edit_text=preview, offset=offset))
    return views


def invalidate(path: Path) -> None:
    """Drop the cached index for *path* (after an in-place edit)."""
    try:
        _cache_file(Path(path)).unlink(missing_ok=True)
    except OSError:
        pass
