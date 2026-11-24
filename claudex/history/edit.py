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
