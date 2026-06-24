"""Unit tests for the fleet state store."""

import os

import pytest

from claudex.fleet.models import Job, JobResult, JobStatus, ProfileCooldown
from claudex.fleet.store import FleetStore, is_pid_alive


@pytest.fixture
def store(tmp_path):
    return FleetStore(root=tmp_path / "fleet")


def test_job_round_trip(store):
    job = Job(prompt="hello", profile="work", model="opus", timeout_s=30)
    store.save_job(job)
    loaded = store.load_job(job.id)
    assert loaded is not None
    assert loaded.prompt == "hello"
    assert loaded.profile == "work"
    assert loaded.model == "opus"
    assert loaded.status == JobStatus.QUEUED
    assert loaded.created_at == job.created_at


def test_resolve_prefix(store):
    job = Job(prompt="x")
    store.save_job(job)
    assert store.resolve_job_id(job.id) == job.id
    assert store.resolve_job_id(job.id[:4]) == job.id
    assert store.resolve_job_id("zzzzzzzz") is None


def test_list_filters(store):
    a = Job(prompt="a", profile="p1", status=JobStatus.RUNNING)
    b = Job(prompt="b", profile="p2", status=JobStatus.QUEUED)
    store.save_job(a)
    store.save_job(b)
    assert {j.id for j in store.list_jobs(status=JobStatus.RUNNING)} == {a.id}
    assert {j.id for j in store.list_jobs(profile="p2")} == {b.id}


def test_compare_and_claim_succeeds(store):
    job = Job(prompt="x", status=JobStatus.QUEUED)
    store.save_job(job)
    claimed = store.compare_and_claim(
        job.id, JobStatus.QUEUED, JobStatus.ASSIGNED, profile="work", bump_attempt=True
    )
    assert claimed is not None
    assert claimed.status == JobStatus.ASSIGNED
    assert claimed.profile == "work"
    assert claimed.attempts == 1


def test_compare_and_claim_fails_when_status_changed(store):
    job = Job(prompt="x", status=JobStatus.QUEUED)
    store.save_job(job)
    # Simulate another process moving it first.
    other = store.load_job(job.id)
    other.status = JobStatus.RUNNING
    store.save_job(other)
    claimed = store.compare_and_claim(job.id, JobStatus.QUEUED, JobStatus.ASSIGNED)
    assert claimed is None


def test_pid_liveness():
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(None) is False
    # An almost-certainly-dead pid.
    assert is_pid_alive(2_000_000_000) is False


def test_result_round_trip(store):
    res = JobResult(job_id="abc", status=JobStatus.SUCCEEDED, result_text="hi", cost_usd=0.01)
    store.save_result(res)
    assert store.has_result("abc")
    loaded = store.load_result("abc")
    assert loaded.result_text == "hi"
    assert loaded.cost_usd == 0.01


def test_cooldowns_round_trip(store):
    from datetime import datetime, timezone

    cd = ProfileCooldown(
        profile="work",
        cooldown_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
        consecutive_rate_limits=2,
    )
    store.save_cooldowns({"work": cd})
    loaded = store.load_cooldowns()
    assert loaded["work"].consecutive_rate_limits == 2
    assert loaded["work"].in_cooldown(datetime(2029, 1, 1, tzinfo=timezone.utc))


def test_attach_pid_only_when_assigned(store):
    job = Job(prompt="x", status=JobStatus.ASSIGNED)
    store.save_job(job)
    store.attach_pid(job.id, 4242)
    assert store.load_job(job.id).pid == 4242
    # If the worker already advanced the job, attach_pid must be a no-op.
    running = store.load_job(job.id)
    running.status = JobStatus.RUNNING
    running.pid = 999
    store.save_job(running)
    store.attach_pid(job.id, 4242)
    after = store.load_job(job.id)
    assert after.status == JobStatus.RUNNING
    assert after.pid == 999  # not clobbered back to the dispatcher's pid


def test_lock_is_reentrant(store):
    # Nested lock() must not deadlock (OS file locks are not reentrant by default).
    with store.lock():
        with store.lock():
            job = Job(prompt="x")
            store.save_job(job)
    assert store.load_job(job.id) is not None


def test_delete_job_removes_artifacts(store):
    job = Job(prompt="x")
    store.save_job(job)
    store.save_result(JobResult(job_id=job.id, status=JobStatus.SUCCEEDED))
    store.log_path(job.id).write_text("log")
    store.delete_job(job.id)
    assert store.load_job(job.id) is None
    assert not store.has_result(job.id)
    assert not store.log_path(job.id).exists()
