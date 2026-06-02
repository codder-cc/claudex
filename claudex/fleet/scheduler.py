"""Rate-limit-aware scheduling and failure classification.

Pure logic (no process spawning) so it is easy to unit-test with an injected
clock. The classifier turns a finished `claude -p` invocation into a failure
kind; the Scheduler chooses which profile runs the next job and tracks per-profile
cooldowns.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from claudex.core.auth import AuthManager
from claudex.core.profile import ProfileManager
from claudex.fleet.models import (
    FAIL_AUTH,
    FAIL_ERROR,
    FAIL_RATE_LIMIT,
    FAIL_TIMEOUT,
    JobStatus,
    ProfileCooldown,
)
from claudex.fleet.store import FleetStore

# Conservative signatures. Checked against combined stdout+stderr (lowercased).
RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "usage limit",
    "quota",
    "too many requests",
    "retry-after",
    "overloaded",
]

AUTH_PATTERNS = [
    "401",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "please log in",
    "oauth token",
    "token expired",
    "credentials",
]

# Cooldown backoff: base seconds, doubled per consecutive rate-limit, capped.
_BACKOFF_BASE_S = 60
_BACKOFF_CAP_S = 3600


class RateLimitClassifier:
    """Classify a finished `claude -p` run into a failure_kind (or None on success)."""

    def classify(
        self, exit_code: int, stdout: str, stderr: str, timed_out: bool = False
    ) -> Optional[str]:
        if timed_out:
            return FAIL_TIMEOUT
        if exit_code == 0 and not self._json_is_error(stdout):
            return None

        # Prefer structured JSON error from --output-format json.
        struct = self._json_error_text(stdout)
        haystack = ((struct or "") + "\n" + stdout + "\n" + stderr).lower()

        if any(p in haystack for p in RATE_LIMIT_PATTERNS):
            return FAIL_RATE_LIMIT
        if any(p in haystack for p in AUTH_PATTERNS):
            return FAIL_AUTH
        return FAIL_ERROR

    def extract_retry_after(self, stdout: str, stderr: str) -> Optional[int]:
        """Parse a Retry-After / 'try again in N seconds' hint, if present."""
        text = stdout + "\n" + stderr
        m = re.search(r"retry[- ]after[:=\s]+(\d+)", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"try again in (\d+)\s*second", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def _json_payload(self, stdout: str) -> Optional[dict]:
        s = stdout.strip()
        if not s.startswith("{"):
            # stream-json / multi-line: try the last JSON object line
            for line in reversed(s.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except Exception:
                        continue
            return None
        try:
            return json.loads(s)
        except Exception:
            return None

    def _json_is_error(self, stdout: str) -> bool:
        payload = self._json_payload(stdout)
        return bool(payload and payload.get("is_error"))

    def _json_error_text(self, stdout: str) -> Optional[str]:
        payload = self._json_payload(stdout)
        if not payload:
            return None
        parts = [
            str(payload.get("subtype", "")),
            str(payload.get("error", "")),
            str(payload.get("result", "")) if payload.get("is_error") else "",
        ]
        return " ".join(p for p in parts if p) or None


class Scheduler:
    def __init__(
        self,
        profile_manager: ProfileManager,
        auth_manager: AuthManager,
        store: FleetStore,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._pm = profile_manager
        self._auth = auth_manager
        self._store = store
        self._clock = clock

    # ── eligibility & selection ──────────────────────────────────────────────
    def _usable_auth(self, profile_name, config_dir) -> bool:
        try:
            status = self._auth.get_status(profile_name, config_dir)
        except Exception:
            return False
        if status.auth_type == "none":
            return False
        if status.auth_type == "api_key":
            return True
        # oauth: ok if not expired, or expired-but-refreshable
        if not status.is_expired:
            return True
        return status.refresh_available

    def eligible_profiles(self) -> list[str]:
        now = self._clock()
        cooldowns = self._store.load_cooldowns()
        out = []
        for p in self._pm.list():
            cd = cooldowns.get(p.name)
            if cd and cd.in_cooldown(now):
                continue
            if not self._usable_auth(p.name, p.config_dir):
                continue
            out.append(p.name)
        return out

    def _active_load(self) -> dict[str, int]:
        """Count of RUNNING/ASSIGNED jobs per profile."""
        load: dict[str, int] = {}
        for status in (JobStatus.RUNNING, JobStatus.ASSIGNED):
            for job in self._store.list_jobs(status=status):
                if job.profile:
                    load[job.profile] = load.get(job.profile, 0) + 1
        return load

    def choose_profile(self, exclude: Optional[set[str]] = None) -> Optional[str]:
        exclude = exclude or set()
        candidates = [p for p in self.eligible_profiles() if p not in exclude]
        if not candidates:
            return None
        load = self._active_load()
        # least-loaded first; tie-break by name for determinism
        candidates.sort(key=lambda name: (load.get(name, 0), name))
        return candidates[0]

    def is_in_cooldown(self, profile: str) -> bool:
        cd = self._store.load_cooldowns().get(profile)
        return bool(cd and cd.in_cooldown(self._clock()))

    # ── token freshness ───────────────────────────────────────────────────--
    def ensure_token_fresh(self, profile_name: str) -> None:
        """Refresh an expired-but-refreshable OAuth token before dispatch.

        Guarded by a per-profile lock so concurrent workers don't both rotate the
        refresh token and clobber .credentials.json (the highest-severity race).
        """
        try:
            profile = self._pm.get(profile_name)
        except Exception:
            return
        with self._store.refresh_lock(profile_name):
            try:
                status = self._auth.get_status(profile_name, profile.config_dir)
            except Exception:
                return
            if status.auth_type == "oauth" and status.is_expired and status.refresh_available:
                try:
                    self._auth.refresh(profile_name, profile.config_dir)
                except Exception:
                    # leave it; the job will fail-auth and retry/cooldown
                    pass

    # ── cooldown bookkeeping ─────────────────────────────────────────────────
    def record_rate_limit(self, profile: str, retry_after_s: Optional[int]) -> None:
        with self._store.lock():
            cooldowns = self._store.load_cooldowns()
            cd = cooldowns.get(profile) or ProfileCooldown(profile=profile)
            cd.consecutive_rate_limits += 1
            backoff = min(
                _BACKOFF_BASE_S * (2 ** (cd.consecutive_rate_limits - 1)), _BACKOFF_CAP_S
            )
            wait = max(retry_after_s or 0, backoff)
            cd.cooldown_until = self._clock() + timedelta(seconds=wait)
            cd.last_failure_kind = FAIL_RATE_LIMIT
            cooldowns[profile] = cd
            self._store.save_cooldowns(cooldowns)

    def record_success(self, profile: str) -> None:
        with self._store.lock():
            cooldowns = self._store.load_cooldowns()
            if profile in cooldowns:
                cooldowns[profile].cooldown_until = None
                cooldowns[profile].consecutive_rate_limits = 0
                self._store.save_cooldowns(cooldowns)
