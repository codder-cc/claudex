"""Data models for session history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
        )


@dataclass
class Message:
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: Optional[datetime] = None
    tokens: Optional[TokenUsage] = None


@dataclass
class Session:
    session_id: str
    project_path: Path
    profile_name: str
    file_path: Path
    started_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    message_count: int = 0
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    title: str = "(untitled)"

    @property
    def age_human(self) -> str:
        # The parser normalises timestamps to naive-local, but be defensive: if an
        # aware datetime ever reaches here, strip the zone so the subtraction below
        # can't raise "can't subtract offset-naive and offset-aware datetimes".
        last_active = self.last_active
        if last_active.tzinfo is not None:
            last_active = last_active.astimezone().replace(tzinfo=None)
        try:
            import humanize
            return humanize.naturaltime(last_active)
        except ImportError:
            delta = datetime.now() - last_active
            hours = int(delta.total_seconds() // 3600)
            if hours < 1:
                return "just now"
            if hours < 24:
                return f"{hours}h ago"
            return f"{hours // 24}d ago"
