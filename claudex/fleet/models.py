"""Fleet data models — Job, JobResult, ProfileCooldown.

All models serialize to/from plain dicts (JSON) with ISO-8601 datetimes, mirroring
the explicit to_dict/from_dict approach used by core.profile.Profile (asdict does
not round-trip datetimes or enums).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s)


def new_job_id() -> str:
    """Short, session-id-like identifier (8 hex chars, prefix-matchable)."""
    return uuid.uuid4().hex[:8]


class JobStatus(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    CANCELLED = "cancelled"


# failure_kind values
FAIL_RATE_LIMIT = "rate_limit"
FAIL_AUTH = "auth"
FAIL_TIMEOUT = "timeout"
FAIL_ERROR = "error"


@dataclass
class Job:
    prompt: str
    id: str = field(default_factory=new_job_id)
    profile: Optional[str] = None  # None = scheduler auto-selects
    status: JobStatus = JobStatus.QUEUED
    parent_id: Optional[str] = None  # set for fan-out children
    is_orchestrator: bool = False  # parent job that owns no process

    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    pid: Optional[int] = None
    attempts: int = 0
    max_attempts: int = 3
    exit_code: Optional[int] = None
    failure_kind: Optional[str] = None  # FAIL_* constants

    cwd: str = field(default_factory=lambda: ".")
    model: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)
    output_format: str = "json"  # json | stream-json | text
    timeout_s: Optional[int] = None

    child_ids: list[str] = field(default_factory=list)  # for orchestrator jobs

    TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    def is_retryable(self) -> bool:
        """True if a non-successful job should be re-queued for another attempt."""
        if self.attempts >= self.max_attempts:
            return False
        if self.status == JobStatus.RATE_LIMITED:
            return True
        if self.status == JobStatus.FAILED and self.failure_kind in (FAIL_AUTH, FAIL_TIMEOUT):
            return True
        return False

    # ── serialization ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "profile": self.profile,
            "status": self.status.value,
            "parent_id": self.parent_id,
            "is_orchestrator": self.is_orchestrator,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "pid": self.pid,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "exit_code": self.exit_code,
            "failure_kind": self.failure_kind,
            "cwd": self.cwd,
            "model": self.model,
            "extra_args": self.extra_args,
            "output_format": self.output_format,
            "timeout_s": self.timeout_s,
            "child_ids": self.child_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            id=data["id"],
            prompt=data["prompt"],
            profile=data.get("profile"),
            status=JobStatus(data.get("status", "queued")),
            parent_id=data.get("parent_id"),
            is_orchestrator=data.get("is_orchestrator", False),
            created_at=_parse(data.get("created_at")) or _now(),
            updated_at=_parse(data.get("updated_at")) or _now(),
            started_at=_parse(data.get("started_at")),
            finished_at=_parse(data.get("finished_at")),
            pid=data.get("pid"),
            attempts=data.get("attempts", 0),
            max_attempts=data.get("max_attempts", 3),
            exit_code=data.get("exit_code"),
            failure_kind=data.get("failure_kind"),
            cwd=data.get("cwd", "."),
            model=data.get("model"),
            extra_args=list(data.get("extra_args", [])),
            output_format=data.get("output_format", "json"),
            timeout_s=data.get("timeout_s"),
            child_ids=list(data.get("child_ids", [])),
        )


@dataclass
class JobResult:
    """Parsed view of a finished job (from `claude -p --output-format json` stdout)."""

    job_id: str
    status: JobStatus
    result_text: str = ""
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    error: Optional[str] = None
    children: list[dict] = field(default_factory=list)  # aggregate for orchestrator

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "result_text": self.result_text,
            "session_id": self.session_id,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "num_turns": self.num_turns,
            "error": self.error,
            "children": self.children,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobResult":
        return cls(
            job_id=data["job_id"],
            status=JobStatus(data.get("status", "failed")),
            result_text=data.get("result_text", ""),
            session_id=data.get("session_id"),
            cost_usd=data.get("cost_usd"),
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            error=data.get("error"),
            children=list(data.get("children", [])),
        )


@dataclass
class ProfileCooldown:
    profile: str
    cooldown_until: Optional[datetime] = None
    last_failure_kind: Optional[str] = None
    consecutive_rate_limits: int = 0

    def in_cooldown(self, now: datetime) -> bool:
        return self.cooldown_until is not None and self.cooldown_until > now

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "cooldown_until": _iso(self.cooldown_until),
            "last_failure_kind": self.last_failure_kind,
            "consecutive_rate_limits": self.consecutive_rate_limits,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileCooldown":
        return cls(
            profile=data["profile"],
            cooldown_until=_parse(data.get("cooldown_until")),
            last_failure_kind=data.get("last_failure_kind"),
            consecutive_rate_limits=data.get("consecutive_rate_limits", 0),
        )
