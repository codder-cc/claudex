"""In-session profile switch mechanics.

Phase 0 scope — PURE mechanics only. Nothing here spawns or kills a process,
and nothing here reads, writes, refreshes or deletes a credential. The only
side effects are:

  * reading a transcript from a *source* profile (read-only, never moved);
  * writing a transcript COPY into a *target* profile's ``projects/`` tree;
  * reading/writing the switch-request handoff file under ~/.claudex.

The Model 1 switch primitive is :func:`carry_transcript`: it drops a Claude
Code transcript (``<sessionId>.jsonl``) into the exact location the target
profile's Claude Code would look for it (mirroring Claude Code's own
``sanitizePath`` + ``resolveSessionFilePath``), stripping account-bound
signature blocks so the target account's first request is not rejected 400.

Reproduces Claude Code's own session-storage behaviour: the project-dir name
encoding and session-file resolution it uses to find a transcript, and the
filtering of account-bound signature blocks (thinking / redacted_thinking /
connector_text) from assistant content while keeping the entries so the
parentUuid chain stays intact.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Claude Code: directory/file name safety. Keep these in lockstep with Claude
# Code's own value — if it changes, resume stops resolving.
MAX_SANITIZED_LENGTH = 200

# Content-block types whose signature is bound to the issuing account's key.
# Filtered from assistant messages on a cross-account carry.
SIGNATURE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "connector_text"})

# Transcripts are owner-only on disk (Claude Code writes 0600).
_TRANSCRIPT_MODE = 0o600


# ---------------------------------------------------------------------------
# Path encoding — mirror of Claude Code sanitizePath()
# ---------------------------------------------------------------------------

def sanitize_project_path(path: str) -> str:
    """Encode an absolute cwd into Claude Code's ``projects/`` directory name.

    ``/Users/foo/my-project`` -> ``-Users-foo-my-project``. Every non-alnum
    char becomes ``-``. NFC-normalised first (Claude normalises cwd to NFC).

    For paths that sanitise to more than :data:`MAX_SANITIZED_LENGTH` chars,
    Claude truncates and appends a hash whose algorithm differs between the
    Bun CLI and the Node SDK — so we return only the stable 200-char prefix and
    rely on :func:`find_project_dir` prefix-matching to locate/avoid collisions.
    Real project paths are far under the cap, so this branch is rare.
    """
    normalized = unicodedata.normalize("NFC", path)
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", normalized)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    return sanitized[:MAX_SANITIZED_LENGTH]


def _is_long_path(path: str) -> bool:
    normalized = unicodedata.normalize("NFC", path)
    return len(re.sub(r"[^a-zA-Z0-9]", "-", normalized)) > MAX_SANITIZED_LENGTH


def projects_dir(config_dir: Path) -> Path:
    return config_dir / "projects"


def find_project_dir(config_dir: Path, cwd: str) -> Optional[Path]:
    """Resolve the on-disk ``projects/<encoded>`` dir for *cwd*, or None.

    Exact match for short paths; prefix scan for long paths (mirrors Claude's
    findProjectDir fallback for the Bun/Node hash mismatch).
    """
    base = projects_dir(config_dir)
    exact = base / sanitize_project_path(cwd)
    if exact.is_dir():
        return exact
    if not _is_long_path(cwd):
        return None
    prefix = sanitize_project_path(cwd)  # already truncated to the cap
    if not base.is_dir():
        return None
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith(prefix + "-"):
            return child
    return None


def target_project_dir(config_dir: Path, cwd: str) -> Path:
    """Where a carried transcript for *cwd* should be written in *config_dir*.

    Reuses an existing matching project dir when present (so we don't create a
    hash-suffix sibling Claude won't look in); otherwise the exact encoded name.
    """
    existing = find_project_dir(config_dir, cwd)
    return existing if existing is not None else projects_dir(config_dir) / sanitize_project_path(cwd)


# ---------------------------------------------------------------------------
# Signature stripping — mirror of Claude Code stripSignatureBlocks()
# ---------------------------------------------------------------------------

def strip_signature_blocks_from_entry(entry: dict) -> tuple[dict, int]:
    """Return (entry, n_removed). Filters signature-bearing blocks out of an
    assistant message's content. Non-assistant / non-list entries pass through.

    The entry object is kept even if its content becomes ``[]`` — Claude's
    resume path absorbs empty content, and keeping the entry preserves the
    parentUuid chain (dropping it would orphan later messages).
    """
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return entry, 0
    message = entry.get("message")
    if not isinstance(message, dict):
        return entry, 0
    content = message.get("content")
    if not isinstance(content, list):
        return entry, 0

    kept = [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") in SIGNATURE_BLOCK_TYPES)
    ]
    removed = len(content) - len(kept)
    if removed == 0:
        return entry, 0

    new_entry = dict(entry)
    new_message = dict(message)
    new_message["content"] = kept
    new_entry["message"] = new_message
    return new_entry, removed


@dataclass
class CarryStats:
    lines_total: int = 0
    lines_json: int = 0
    lines_passthrough: int = 0  # non-JSON lines copied verbatim
    entries_stripped: int = 0
    blocks_stripped: int = 0
    stripped: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def rewrite_transcript(src: Path, dst: Path, strip: bool = True) -> CarryStats:
    """Read JSONL transcript *src*, write to *dst*, optionally stripping
    signature blocks. *src* is only read. *dst* is created 0600.

    Malformed (non-JSON) lines are copied verbatim rather than dropped — a
    single bad line must never truncate a resumable transcript.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    stats = CarryStats(stripped=strip)
    # newline="" + explicit "\n" keeps byte-for-byte line discipline.
    with open(src, "r", encoding="utf-8", errors="replace", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        for raw in fin:
            line = raw.rstrip("\n")
            stats.lines_total += 1
            if not line.strip():
                fout.write(raw)
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                stats.lines_passthrough += 1
                fout.write(raw)
                continue
            stats.lines_json += 1
            if strip and isinstance(obj, dict):
                obj, removed = strip_signature_blocks_from_entry(obj)
                if removed:
                    stats.entries_stripped += 1
                    stats.blocks_stripped += removed
            fout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        dst.chmod(_TRANSCRIPT_MODE)
    except OSError:
        pass
    return stats


# ---------------------------------------------------------------------------
# Transcript carry (Model 1 primitive)
# ---------------------------------------------------------------------------

def locate_source_transcript(
    source_config_dir: Path,
    session_id: str,
    cwd: Optional[str] = None,
) -> Optional[Path]:
    """Find ``<session_id>.jsonl`` in the source profile. Prefers the cwd's
    encoded project dir; falls back to scanning all project dirs. Zero-byte
    files are treated as not-found (truncated copy). Read-only."""
    name = f"{session_id}.jsonl"
    if cwd:
        pdir = find_project_dir(source_config_dir, cwd)
        if pdir is not None:
            cand = pdir / name
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
    base = projects_dir(source_config_dir)
    if not base.is_dir():
        return None
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        cand = child / name
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    return None


def resolve_transcript(
    config_dir: Path,
    session_id: str,
    cwd: Optional[str] = None,
) -> Optional[Path]:
    """Mirror of resolveSessionFilePath: would ``claude --resume <session_id>``
    (launched with CLAUDE_CONFIG_DIR=config_dir) find a transcript? Used to
    verify a carry landed where Claude Code will look."""
    return locate_source_transcript(config_dir, session_id, cwd)


def carry_project_config(
    source_config_dir: Path,
    target_config_dir: Path,
    cwd: str,
    trust: bool = True,
) -> bool:
    """Carry the per-project entry for *cwd* from source's ``.claude.json`` into
    target's, so the switched-in profile doesn't re-prompt the folder-trust
    dialog and keeps tool approvals.

    Only ``projects[cwd]`` is touched — top-level identity (``oauthAccount``,
    ``userID``, ``hasCompletedOnboarding``) and every other project are preserved
    byte-for-byte in content. Carries ``allowedTools`` (union) and MD-includes
    approval; forces ``hasTrustDialogAccepted`` when *trust*. Metrics/lastSessionId
    are left to the target. Returns True if written.

    Defensive: if target's ``.claude.json`` exists but doesn't parse, bails
    without writing (never risks corrupting the identity file). No credentials
    or network involved. Writes atomically (temp + rename), mode 0600.
    """
    dst = target_config_dir / ".claude.json"
    data: Optional[dict] = None
    if dst.exists():
        try:
            data = json.loads(dst.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False  # unparseable identity file — do not clobber
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return False

    src_entry: dict = {}
    src = source_config_dir / ".claude.json"
    if src.exists():
        try:
            sdata = json.loads(src.read_text(encoding="utf-8"))
            src_entry = (sdata.get("projects") or {}).get(cwd) or {}
        except (OSError, ValueError, json.JSONDecodeError):
            src_entry = {}
    if not isinstance(src_entry, dict):
        src_entry = {}

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return False
    entry = dict(projects.get(cwd) or {})

    src_tools = src_entry.get("allowedTools")
    if isinstance(src_tools, list):
        existing = entry.get("allowedTools") if isinstance(entry.get("allowedTools"), list) else []
        entry["allowedTools"] = list(dict.fromkeys(existing + src_tools))
    if src_entry.get("hasClaudeMdExternalIncludesApproved"):
        entry["hasClaudeMdExternalIncludesApproved"] = True
    if trust:
        entry["hasTrustDialogAccepted"] = True
    projects[cwd] = entry

    tmp = dst.with_name(dst.name + ".claudex-tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(dst)
    return True


@dataclass
class CarryResult:
    source: Path
    target: Path
    session_id: str
    stats: CarryStats


def carry_transcript(
    source_config_dir: Path,
    target_config_dir: Path,
    session_id: str,
    cwd: str,
    strip: bool = True,
) -> CarryResult:
    """Copy ``<session_id>.jsonl`` from source profile to target profile at the
    path Claude Code resolves for *cwd*, stripping signature blocks by default.

    Source is READ ONLY — never moved or mutated. No credential is touched.
    Raises FileNotFoundError if the source transcript can't be located.
    """
    src = locate_source_transcript(source_config_dir, session_id, cwd)
    if src is None:
        raise FileNotFoundError(
            f"transcript {session_id}.jsonl not found in {projects_dir(source_config_dir)}"
        )
    dst = target_project_dir(target_config_dir, cwd) / f"{session_id}.jsonl"
    stats = rewrite_transcript(src, dst, strip=strip)
    return CarryResult(source=src, target=dst, session_id=session_id, stats=stats)


# ---------------------------------------------------------------------------
# Switch-request handoff (hook -> supervisor)
# ---------------------------------------------------------------------------

# Trigger is a PLAIN prompt, not a slash command. Claude Code's slash router
# (processUserInput.ts) short-circuits unknown slash commands BEFORE the
# UserPromptSubmit hook runs (returns shouldQuery=false), so `/switch` never
# reaches the hook. A normal prompt does. We require a SINGLE-token argument so
# ordinary sentences like "switch the variable names" are not captured. A
# leading slash is tolerated (harmless) in case a future version routes it here.
_SWITCH_CMD_RE = re.compile(r"^\s*/?switch\s+(?P<arg>\S+)\s*$", re.IGNORECASE)


def parse_switch_command(prompt: str) -> Optional[str]:
    """Extract the target token from a ``switch <target>`` prompt, else None.

    The target is a single token: a profile name or index, or a comma-list like
    ``1,3,5`` (multi-switch handled downstream). Multi-word input is not a
    switch and passes through to the model untouched.
    """
    if not isinstance(prompt, str):
        return None
    m = _SWITCH_CMD_RE.match(prompt)
    if not m:
        return None
    arg = m.group("arg").strip()
    return arg or None


@dataclass
class SwitchRequest:
    target: str
    session_id: str
    cwd: str
    source_profile: str = ""
    transcript_path: str = ""
    model: int = 1  # 1 = isolated dir carry, 2 = shared-dir account swap
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "SwitchRequest":
        data = json.loads(text)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def write(self, path: Path) -> None:
        """Atomically write the request (temp + rename), owner-only."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)

    @classmethod
    def read(cls, path: Path) -> Optional["SwitchRequest"]:
        if not path.exists():
            return None
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
