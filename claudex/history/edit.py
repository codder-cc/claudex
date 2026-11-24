"""Trim a Claude Code transcript — drop the last N conversational rounds.

A Claude Code ``*.jsonl`` transcript is a linear chain of entries linked by
``parentUuid``. A single *visible* exchange ("я спросил → ассистент ответил")
is usually **several** physical lines: the assistant's ``text`` / ``thinking`` /
``tool_use`` blocks each land on their own line, and every tool call comes back
as an extra ``user`` line carrying a ``tool_result`` (``toolUseResult``). So
"delete the last N messages" only has a safe meaning at the level of a *round*:

    round = one real user message + everything the assistant did in reply,
            up to (but not including) the next real user message.

Trimming whole rounds from the tail always cuts on a clean boundary — the
retained transcript ends right after a completed assistant turn, so there is
never a dangling ``tool_use`` without its ``tool_result`` and ``--resume`` keeps
working. (Cutting at an arbitrary line can orphan a ``tool_use`` and make Claude
reject the transcript on the next request, which is why we don't offer that.)

Everything here only reads and rewrites the one JSONL file; a timestamped
``.bak`` copy is taken before the file is replaced atomically.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_TRANSCRIPT_MODE = 0o600
_MSG_TYPES = ("user", "assistant")


# ── entry helpers ─────────────────────────────────────────────────────────────
@dataclass
class _Entry:
    """One parsed JSONL line plus its original raw text (byte-preserving)."""
    raw: str
    obj: Optional[dict]
    dirty: bool = False   # obj was mutated → re-serialise instead of writing raw

    @property
    def uuid(self) -> Optional[str]:
        return self.obj.get("uuid") if isinstance(self.obj, dict) else None

    @property
    def parent(self) -> Optional[str]:
        return self.obj.get("parentUuid") if isinstance(self.obj, dict) else None

    @property
    def type(self) -> Optional[str]:
        return self.obj.get("type") if isinstance(self.obj, dict) else None


def _is_tool_result(obj: dict) -> bool:
    """A ``user`` line that is really a tool result, not something the human typed."""
    if "toolUseResult" in obj:
        return True
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


_COMMAND_PREFIXES = ("<command-name>", "<command-message>", "<command-args>",
                     "<local-command-stdout>", "<local-command-caveat>")


def _is_command_plumbing(obj: dict) -> bool:
    """A ``user`` line that is slash-command markup (``/exit``, stdout echoes).

    Claude Code records these as user messages, but they are harness plumbing,
    not something the human conversationally said, so they don't start a round.
    """
    msg = obj.get("message") if isinstance(obj, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        content = next((b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), "")
    if not isinstance(content, str):
        return False
    return content.lstrip().startswith(_COMMAND_PREFIXES)


def is_user_turn(obj: dict) -> bool:
    """True for a genuine human message — the boundary that starts a round.

    Excludes tool-result ``user`` lines, harness ``isMeta`` injections (the
    "Caveat: …" lines), subagent sidechains, and slash-command plumbing.
    """
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return False
    if obj.get("isMeta") or obj.get("isSidechain"):
        return False
    if _is_command_plumbing(obj):
        return False
    return not _is_tool_result(obj)


def _message_text(obj: dict, limit: int = 100) -> str:
    """Short one-line snippet of an entry's text, for previews."""
    msg = obj.get("message") if isinstance(obj, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
    text = " ".join(text.split())
    return text[:limit]


# ── loading + active chain ────────────────────────────────────────────────────
def _load(path: Path) -> List[_Entry]:
    entries: List[_Entry] = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                entries.append(_Entry(raw=raw, obj=None))
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                obj = None
            entries.append(_Entry(raw=raw, obj=obj if isinstance(obj, dict) else None))
    return entries


def _active_chain(entries: List[_Entry]) -> List[dict]:
    """The resumable chain, root→leaf, following ``parentUuid`` from the leaf.

    The leaf is the last non-sidechain message line in file order (what Claude
    resumes from). Dead branches and subagent sidechains are left out of the
    chain so trimming never touches them.
    """
    by_uuid: dict = {}
    leaf: Optional[str] = None
    for e in entries:
        if e.obj is None or e.type not in _MSG_TYPES:
            continue
        u = e.uuid
        if not u:
            continue
        by_uuid[u] = e.obj
        if not e.obj.get("isSidechain"):
            leaf = u
    chain: List[dict] = []
    seen: set = set()
    cur = leaf
    while cur and cur in by_uuid and cur not in seen:
        seen.add(cur)
        chain.append(by_uuid[cur])
        cur = by_uuid[cur].get("parentUuid")
    chain.reverse()
    return chain


# ── rounds ────────────────────────────────────────────────────────────────────
@dataclass
class Round:
    index: int                 # 0-based position from the start of the conversation
    user_uuid: str
    user_text: str
    uuids: List[str]           # every entry uuid in this round (user + assistant + results)
    assistant_text: str        # last assistant text block in the round (reply snippet)
    line_count: int


def list_rounds(path: Path) -> List[Round]:
    """Split a transcript's active chain into conversational rounds, in order."""
    chain = _active_chain(_load(path))
    rounds: List[Round] = []
    cur: Optional[Round] = None
    for obj in chain:
        u = obj.get("uuid")
        if is_user_turn(obj):
            cur = Round(index=len(rounds), user_uuid=u or "", user_text=_message_text(obj),
                        uuids=[], assistant_text="", line_count=0)
            rounds.append(cur)
        if cur is None:
            # Leading non-user entries (rare) — fold into an implicit round 0.
            cur = Round(index=0, user_uuid="", user_text="(session start)",
                        uuids=[], assistant_text="", line_count=0)
            rounds.append(cur)
        if u:
            cur.uuids.append(u)
        cur.line_count += 1
        if obj.get("type") == "assistant":
            snippet = _message_text(obj)
            if snippet:
                cur.assistant_text = snippet
    return rounds


# ── trim plan + apply ─────────────────────────────────────────────────────────
@dataclass
class TrimPlan:
    rounds_total: int
    rounds_removed: int
    removed_uuids: set = field(default_factory=set)
    new_leaf_uuid: Optional[str] = None
    lines_before: int = 0
    lines_after: int = 0
    removed_rounds: List[Round] = field(default_factory=list)

    @property
    def rounds_kept(self) -> int:
        return self.rounds_total - self.rounds_removed


class TrimError(ValueError):
    """Raised when a trim request cannot be satisfied safely."""


def plan_trim_rounds(path: Path, n: int) -> TrimPlan:
    """Work out exactly which entries dropping the last *n* rounds removes.

    Pure analysis — nothing is written. Raises :class:`TrimError` if *n* is not
    a positive number, exceeds the available rounds, or would empty the file.
    """
    if n <= 0:
        raise TrimError("number of rounds to trim must be ≥ 1")
    entries = _load(path)
    rounds = list_rounds(path)
    total = len(rounds)
    if total == 0:
        raise TrimError("transcript has no conversational rounds to trim")
    if n >= total:
        raise TrimError(
            f"asked to trim {n} round(s) but the session only has {total}; "
            f"trimming all of them would empty it — delete the session instead "
            f"(claudex session delete) if that's what you want")
    cut = rounds[total - n:]
    removed_uuids = {u for r in cut for u in r.uuids}
    keep_chain = [o for o in _active_chain(entries)
                  if o.get("uuid") not in removed_uuids]
    new_leaf = keep_chain[-1].get("uuid") if keep_chain else None

    lines_after = 0
    for e in entries:
        if e.obj is not None and (e.uuid in removed_uuids or e.parent in removed_uuids):
            continue
        lines_after += 1
    return TrimPlan(
        rounds_total=total, rounds_removed=n, removed_uuids=removed_uuids,
        new_leaf_uuid=new_leaf, lines_before=len(entries), lines_after=lines_after,
        removed_rounds=cut,
    )


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_suffix(path.suffix + f".bak-{stamp}")


def apply_trim(path: Path, plan: TrimPlan, *, backup: bool = True) -> Optional[Path]:
    """Rewrite *path* with the planned rounds removed, atomically.

    Drops any line whose ``uuid`` — or whose ``parentUuid`` — is in the removed
    set, which sweeps up the message lines, their ``tool_result`` replies, and
    the ``progress`` / snapshot lines that referenced them. Returns the backup
    path (or ``None`` when ``backup=False``).
    """
    bak: Optional[Path] = None
    if backup:
        bak = _backup_path(path)
        shutil.copy2(path, bak)

    entries = _load(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as out:
        for e in entries:
            if e.obj is not None and (
                e.uuid in plan.removed_uuids or e.parent in plan.removed_uuids
            ):
                continue
            out.write(e.raw if e.raw.endswith("\n") else e.raw + "\n")
    try:
        tmp.chmod(_TRANSCRIPT_MODE)
    except OSError:
        pass
    tmp.replace(path)
    return bak


def trim_rounds(path: Path, n: int, *, backup: bool = True,
                dry_run: bool = False) -> tuple[TrimPlan, Optional[Path]]:
    """Convenience: plan + apply. Returns ``(plan, backup_path)``.

    With ``dry_run=True`` nothing is written and the backup path is ``None``.
    """
    plan = plan_trim_rounds(path, n)
    if dry_run:
        return plan, None
    bak = apply_trim(path, plan, backup=backup)
    return plan, bak


# ── message-level view + edit ─────────────────────────────────────────────────
@dataclass
class MsgView:
    """A single message entry, flattened for display and editing."""
    uuid: str
    role: str                  # "user" | "assistant"
    kind: str                  # text | thinking | tool_use | tool_result | command | meta
    text: str                  # full text for editable kinds; a label otherwise
    round_index: int           # which conversational round it belongs to (0-based)
    editable: bool             # True for plain text user / assistant messages
    on_active_chain: bool      # False for dead branches / sidechains

    @property
    def preview(self) -> str:
        return " ".join(self.text.split())[:80]


def _entry_kind(obj: dict) -> str:
    if obj.get("isMeta"):
        return "meta"
    if _is_command_plumbing(obj):
        return "command"
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return "text"
    if isinstance(content, list):
        kinds = [b.get("type") for b in content if isinstance(b, dict)]
        for k in ("tool_result", "tool_use", "thinking", "text"):
            if k in kinds:
                return k
    return "text"


def _entry_full_text(obj: dict) -> str:
    """The editable text of a message, or a human label for non-text kinds."""
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "")
        for b in content:
            if isinstance(b, dict) and b.get("type") == "thinking":
                return b.get("thinking", "")
        # non-text: summarise
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                return f"[tool_use {b.get('name', '?')}] {json.dumps(b.get('input', {}))[:200]}"
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else json.dumps(c)[:200]
                return f"[tool_result] {txt[:200]}"
    return ""


def _set_entry_text(obj: dict, new_text: str) -> bool:
    """Replace a message's text in place. Returns False if it has no text slot."""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = new_text
        return True
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                b["text"] = new_text
                return True
        for b in content:
            if isinstance(b, dict) and b.get("type") == "thinking":
                b["thinking"] = new_text
                return True
    return False


def list_messages(path: Path, *, include_inactive: bool = False) -> List[MsgView]:
    """Flatten a transcript into per-message views, in file order.

    By default only the active (resumable) chain is returned; pass
    ``include_inactive=True`` to also surface dead branches / sidechains.
    """
    entries = _load(path)
    chain_uuids = {o.get("uuid") for o in _active_chain(entries)}
    # map uuid → round index
    round_of: dict = {}
    for r in list_rounds(path):
        for u in r.uuids:
            round_of[u] = r.index
    views: List[MsgView] = []
    for e in entries:
        if e.obj is None or e.type not in _MSG_TYPES:
            continue
        u = e.uuid
        if not u:
            continue
        active = u in chain_uuids
        if not active and not include_inactive:
            continue
        kind = _entry_kind(e.obj)
        views.append(MsgView(
            uuid=u, role=e.type or "", kind=kind, text=_entry_full_text(e.obj),
            round_index=round_of.get(u, -1),
            editable=(kind in ("text", "thinking")),
            on_active_chain=active,
        ))
    return views


def _commit(path: Path, entries: List[_Entry], *, backup: bool) -> Optional[Path]:
    """Write *entries* back atomically, re-serialising any marked ``dirty``."""
    bak: Optional[Path] = None
    if backup:
        bak = _backup_path(path)
        shutil.copy2(path, bak)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as out:
        for e in entries:
            if e.obj is None:                       # blank / unparseable: verbatim
                out.write(e.raw if e.raw.endswith("\n") else e.raw + "\n")
                continue
            if e.dirty:
                out.write(json.dumps(e.obj, ensure_ascii=False,
                                     separators=(",", ":")) + "\n")
            else:
                out.write(e.raw if e.raw.endswith("\n") else e.raw + "\n")
    try:
        tmp.chmod(_TRANSCRIPT_MODE)
    except OSError:
        pass
    tmp.replace(path)
    return bak


def edit_message_text(path: Path, uuid: str, new_text: str, *,
                      backup: bool = True) -> Optional[Path]:
    """Replace the text of one message (user or assistant) in place.

    Only the message's text changes; the ``parentUuid`` chain is untouched, so
    the transcript stays resumable. Raises :class:`TrimError` if *uuid* is not a
    message with an editable text slot.
    """
    entries = _load(path)
    for e in entries:
        if e.obj is not None and e.uuid == uuid:
            if e.type not in _MSG_TYPES or not _set_entry_text(e.obj, new_text):
                raise TrimError(f"message {uuid[:8]} has no editable text")
            e.dirty = True
            return _commit(path, entries, backup=backup)
    raise TrimError(f"message {uuid[:8]} not found")


def _deletion_group(entries: List[_Entry], uuid: str) -> set:
    """UUIDs removed together with *uuid* — a tool_use and its tool_result are an
    inseparable pair, so deleting one deletes both."""
    by_uuid = {e.uuid: e for e in entries if e.obj is not None and e.uuid}
    target = by_uuid.get(uuid)
    if target is None:
        raise TrimError(f"message {uuid[:8]} not found")
    group = {uuid}
    kind = _entry_kind(target.obj)
    if kind == "tool_use":
        # its result is the child user line carrying the same tool_use_id
        ids = {b.get("id") for b in (target.obj.get("message", {}).get("content") or [])
               if isinstance(b, dict) and b.get("type") == "tool_use"}
        for e in entries:
            if e.obj is None or e.type != "user":
                continue
            for b in (e.obj.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in ids:
                    group.add(e.uuid)
    elif kind == "tool_result":
        group.add(target.parent)   # the tool_use assistant line is the parent
    group.discard(None)
    return group


def delete_message(path: Path, uuid: str, *, backup: bool = True) -> Optional[Path]:
    """Delete one message from the active chain, re-linking around the gap.

    The successor's ``parentUuid`` is spliced onto the deleted node's parent so
    the chain stays unbroken and resumable. A ``tool_use`` and its ``tool_result``
    are always removed together (deleting half would orphan the other).
    """
    entries = _load(path)
    chain = _active_chain(entries)
    chain_pos = {o.get("uuid"): i for i, o in enumerate(chain)}
    if uuid not in chain_pos:
        raise TrimError(f"message {uuid[:8]} is not on the active chain")
    group = _deletion_group(entries, uuid)
    positions = sorted(chain_pos[u] for u in group if u in chain_pos)
    first_pos, last_pos = positions[0], positions[-1]
    new_parent = chain[first_pos].get("parentUuid")        # parent of the group head
    # Re-link the active-chain successor (if any) onto new_parent.
    if last_pos + 1 < len(chain):
        succ_uuid = chain[last_pos + 1].get("uuid")
        for e in entries:
            if e.obj is not None and e.uuid == succ_uuid:
                e.obj["parentUuid"] = new_parent
                e.dirty = True
                break
    # Drop the group and anything (tool results, progress) still pointing at it,
    # but never the successor we just re-linked.
    kept = [e for e in entries
            if e.obj is None or (e.uuid not in group and e.parent not in group)]
    return _commit(path, kept, backup=backup)


def truncate_to(path: Path, uuid: str, *, backup: bool = True) -> Optional[Path]:
    """Keep the transcript up to and including *uuid*; drop everything after it
    on the active chain. A convenient "trim from here down" for the editor."""
    entries = _load(path)
    chain = _active_chain(entries)
    pos = {o.get("uuid"): i for i, o in enumerate(chain)}
    if uuid not in pos:
        raise TrimError(f"message {uuid[:8]} is not on the active chain")
    removed = {o.get("uuid") for o in chain[pos[uuid] + 1:]}
    if not removed:
        return None   # already the leaf → nothing to do
    kept = [e for e in entries
            if e.obj is None or (e.uuid not in removed and e.parent not in removed)]
    return _commit(path, kept, backup=backup)


def read_since(path: Path, offset: int) -> tuple[List[dict], int]:
    """For live-follow: return message objects appended past byte *offset*,
    plus the new end offset. Partial trailing lines are not consumed."""
    out: List[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        f.seek(offset)
        consumed = offset
        for raw in f:
            if not raw.endswith("\n"):
                break   # partial line still being written — stop, re-read next tick
            consumed += len(raw.encode("utf-8", "replace"))
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("type") in _MSG_TYPES:
                out.append(obj)
    return out, consumed
