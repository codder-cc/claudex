"""Unit tests for the rate-limit classifier and scheduler."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from claudex.fleet.models import (
    FAIL_AUTH,
    FAIL_ERROR,
    FAIL_RATE_LIMIT,
    FAIL_TIMEOUT,
    Job,
    JobStatus,
)
from claudex.fleet.scheduler import RateLimitClassifier, Scheduler
from claudex.fleet.store import FleetStore


# ── classifier ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exit_code,stdout,stderr,timed_out,expected",
    [
        (0, '{"is_error": false, "result": "ok"}', "", False, None),
        (1, '{"is_error": true, "result": "rate limit exceeded"}', "", False, FAIL_RATE_LIMIT),
        (1, "", "HTTP 429 Too Many Requests", False, FAIL_RATE_LIMIT),
        (1, "", "Error 401: unauthorized", False, FAIL_AUTH),
        (1, '{"is_error": true, "result": "please log in"}', "", False, FAIL_AUTH),
        (2, "", "boom segfault", False, FAIL_ERROR),
        (124, "", "", True, FAIL_TIMEOUT),
        (0, '{"is_error": true, "result": "usage limit reached"}', "", False, FAIL_RATE_LIMIT),
    ],
)
def test_classify(exit_code, stdout, stderr, timed_out, expected):
    assert RateLimitClassifier().classify(exit_code, stdout, stderr, timed_out) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Retry-After: 30", 30),
        ("please try again in 12 seconds", 12),
        ("no hint here", None),
    ],
)
def test_extract_retry_after(text, expected):
    assert RateLimitClassifier().extract_retry_after("", text) == expected


# ── scheduler with fakes + fake clock ────────────────────────────────────────
@dataclass
class FakeProfile:
    name: str
    config_dir: Path
    email: str = ""


@dataclass
class FakeStatus:
    auth_type: str
    is_expired: bool = False
    refresh_available: bool = False
    expires_in_human: str = "—"


class FakePM:
    def __init__(self, profiles):
        self._profiles = profiles

    def list(self):
        return self._profiles

    def get(self, name):
        for p in self._profiles:
            if p.name == name:
                return p
        raise KeyError(name)


class FakeAuth:
    def __init__(self, statuses):
        self._statuses = statuses
        self.refreshed = []

    def get_status(self, name, config_dir):
        return self._statuses[name]

    def refresh(self, name, config_dir):
        self.refreshed.append(name)
        self._statuses[name].is_expired = False


@pytest.fixture
def clock():
    state = {"now": datetime(2030, 1, 1, tzinfo=timezone.utc)}

    def _clock():
        return state["now"]

    _clock.state = state
    return _clock


def _make(tmp_path, profiles, statuses, clock):
    store = FleetStore(root=tmp_path / "fleet")
    pm = FakePM(profiles)
    auth = FakeAuth(statuses)
    sched = Scheduler(pm, auth, store, clock=clock)
    return store, pm, auth, sched


def test_eligible_excludes_unusable_auth(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a"), FakeProfile("b", tmp_path / "b")]
    statuses = {
        "a": FakeStatus("oauth", is_expired=False),
        "b": FakeStatus("none"),
    }
    _, _, _, sched = _make(tmp_path, profiles, statuses, clock)
    assert sched.eligible_profiles() == ["a"]


def test_eligible_includes_expired_but_refreshable(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a")]
    statuses = {"a": FakeStatus("oauth", is_expired=True, refresh_available=True)}
    _, _, _, sched = _make(tmp_path, profiles, statuses, clock)
    assert sched.eligible_profiles() == ["a"]


def test_eligible_excludes_expired_non_refreshable(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a")]
    statuses = {"a": FakeStatus("oauth", is_expired=True, refresh_available=False)}
    _, _, _, sched = _make(tmp_path, profiles, statuses, clock)
    assert sched.eligible_profiles() == []


def test_cooldown_excludes_and_expires(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a")]
    statuses = {"a": FakeStatus("api_key")}
    _, _, _, sched = _make(tmp_path, profiles, statuses, clock)
    sched.record_rate_limit("a", retry_after_s=None)
    assert sched.eligible_profiles() == []  # now in cooldown
    clock.state["now"] += timedelta(hours=2)  # past backoff
    assert sched.eligible_profiles() == ["a"]


def test_backoff_grows(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a")]
    statuses = {"a": FakeStatus("api_key")}
    store, _, _, sched = _make(tmp_path, profiles, statuses, clock)
    sched.record_rate_limit("a", None)
    first = store.load_cooldowns()["a"].cooldown_until
    sched.record_rate_limit("a", None)
    second = store.load_cooldowns()["a"].cooldown_until
    assert (second - clock.state["now"]) > (first - clock.state["now"])


def test_choose_least_loaded(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a"), FakeProfile("b", tmp_path / "b")]
    statuses = {"a": FakeStatus("api_key"), "b": FakeStatus("api_key")}
    store, _, _, sched = _make(tmp_path, profiles, statuses, clock)
    # a already has a running job → b should be chosen
    store.save_job(Job(prompt="x", profile="a", status=JobStatus.RUNNING))
    assert sched.choose_profile() == "b"


def test_ensure_token_fresh_refreshes(tmp_path, clock):
    profiles = [FakeProfile("a", tmp_path / "a")]
    statuses = {"a": FakeStatus("oauth", is_expired=True, refresh_available=True)}
    _, _, auth, sched = _make(tmp_path, profiles, statuses, clock)
    sched.ensure_token_fresh("a")
    assert auth.refreshed == ["a"]
