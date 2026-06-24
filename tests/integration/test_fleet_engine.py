"""Integration tests for the fleet engine and worker (no real `claude`)."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudex.fleet.engine import FleetEngine
from claudex.fleet.models import FAIL_ERROR, Job, JobResult, JobStatus
from claudex.fleet.store import FleetStore


@dataclass
class FakeProfile:
    name: str
    config_dir: Path
    email: str = ""


@dataclass
class FakeStatus:
    auth_type: str = "api_key"
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
    def get_status(self, name, config_dir):
        return FakeStatus()

    def get_env_for_profile(self, name, config_dir):
        return {"CLAUDE_CONFIG_DIR": str(config_dir)}

    def refresh(self, name, config_dir):
        return FakeStatus()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    profiles = [FakeProfile("a", tmp_path / "a"), FakeProfile("b", tmp_path / "b")]
    store = FleetStore(root=tmp_path / "fleet")
    eng = FleetEngine(store=store, profile_manager=FakePM(profiles), auth_manager=FakeAuth())
    return eng


def test_dispatch_schedules_and_spawns(engine, monkeypatch):
    spawned = {}

    def fake_spawn(store, job):
        spawned["job"] = job.id
        return 4242

    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", fake_spawn)
    job = engine.dispatch("do a thing")
    assert job.status == JobStatus.ASSIGNED
    assert job.profile in ("a", "b")
    assert job.pid == 4242
    assert spawned["job"] == job.id


def test_reconcile_finalizes_from_result(engine, monkeypatch):
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 999)
    job = engine.dispatch("x", profile="a")
    # Worker "finished": write result and leave status RUNNING.
    running = engine.store.load_job(job.id)
    running.status = JobStatus.RUNNING
    engine.store.save_job(running)
    engine.store.save_result(
        JobResult(job_id=job.id, status=JobStatus.SUCCEEDED, result_text="done")
    )
    engine.tick()
    assert engine.store.load_job(job.id).status == JobStatus.SUCCEEDED


def test_reconcile_dead_pid_fails(engine, monkeypatch):
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 2_000_000_000)
    job = engine.dispatch("x", profile="a")
    running = engine.store.load_job(job.id)
    running.status = JobStatus.RUNNING
    running.pid = 2_000_000_000  # dead
    running.started_at = datetime.now(timezone.utc)
    engine.store.save_job(running)
    engine.tick()
    finalized = engine.store.load_job(job.id)
    assert finalized.status == JobStatus.FAILED
    assert finalized.failure_kind == FAIL_ERROR


def test_rate_limited_requeues_after_cooldown(engine, monkeypatch):
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 1)
    job = engine.dispatch("x", profile="a", auto_tick=False)
    j = engine.store.load_job(job.id)
    j.status = JobStatus.RATE_LIMITED
    j.attempts = 1
    j.profile = "a"
    engine.store.save_job(j)
    # No active cooldown recorded → retry path should re-queue it.
    engine.tick()
    requeued = engine.store.load_job(job.id)
    assert requeued.status in (JobStatus.QUEUED, JobStatus.ASSIGNED)


def test_max_concurrent_caps_starts(tmp_path, monkeypatch):
    profiles = [FakeProfile("a", tmp_path / "a")]
    store = FleetStore(root=tmp_path / "fleet")
    eng = FleetEngine(
        store=store, profile_manager=FakePM(profiles), auth_manager=FakeAuth(), max_concurrent=2
    )
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 1)
    for _ in range(5):
        eng.dispatch("x", profile="a", auto_tick=False)
    eng.tick()
    started = len(store.list_jobs(status=JobStatus.ASSIGNED)) + len(
        store.list_jobs(status=JobStatus.RUNNING)
    )
    assert started == 2


def test_fanout_rollup(engine, monkeypatch):
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 1)
    parent = engine.fan_out("parent", ["s1", "s2"])
    assert len(parent.child_ids) == 2
    # Mark all children succeeded with results.
    for cid in parent.child_ids:
        c = engine.store.load_job(cid)
        c.status = JobStatus.SUCCEEDED
        engine.store.save_job(c)
        engine.store.save_result(
            JobResult(job_id=cid, status=JobStatus.SUCCEEDED, result_text=f"out-{cid}")
        )
    engine.tick()
    rolled = engine.store.load_job(parent.id)
    assert rolled.status == JobStatus.SUCCEEDED
    res = engine.store.load_result(parent.id)
    assert len(res.children) == 2


def test_cancel_queued_job(engine):
    job = engine.dispatch("x", profile="a", auto_tick=False)
    cancelled = engine.cancel(job.id)
    assert cancelled.status == JobStatus.CANCELLED


def test_orchestrators_do_not_consume_worker_budget(tmp_path, monkeypatch):
    # Regression: in-flight orchestrators (RUNNING, no process) must not eat the
    # concurrency budget, or fan-outs deadlock.
    profiles = [FakeProfile("a", tmp_path / "a")]
    store = FleetStore(root=tmp_path / "fleet")
    eng = FleetEngine(
        store=store, profile_manager=FakePM(profiles), auth_manager=FakeAuth(), max_concurrent=2
    )
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 1)
    # Two orchestrators sitting RUNNING (e.g. mid fan-out).
    for _ in range(2):
        o = Job(prompt="parent", is_orchestrator=True, status=JobStatus.RUNNING)
        store.save_job(o)
    # A real queued child should still be scheduled despite max_concurrent=2.
    job = eng.dispatch("child", profile="a")
    assert job.status == JobStatus.ASSIGNED


def test_reconcile_dead_assigned_fails(engine, monkeypatch):
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 2_000_000_000)
    job = engine.dispatch("x", profile="a", auto_tick=False)
    j = engine.store.load_job(job.id)
    j.status = JobStatus.ASSIGNED
    j.pid = 2_000_000_000  # dead, never reached RUNNING
    engine.store.save_job(j)
    engine.tick()
    assert engine.store.load_job(job.id).status == JobStatus.FAILED


def test_cancel_cascades_to_children(engine, monkeypatch):
    # Use a dead pid so cancel doesn't try to signal a real process.
    monkeypatch.setattr("claudex.fleet.runner.spawn_detached", lambda s, j: 2_000_000_000)
    parent = engine.fan_out("p", ["s1", "s2"])
    engine.cancel(parent.id)
    assert engine.store.load_job(parent.id).status == JobStatus.CANCELLED
    for cid in parent.child_ids:
        assert engine.store.load_job(cid).status == JobStatus.CANCELLED


# ── real worker with a fake `claude` binary ─────────────────────────────────
@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'echo \'{"is_error": false, "result": "hi from fake", '
        '"session_id": "s1", "total_cost_usd": 0.02, "num_turns": 1}\'\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return script


@pytest.mark.skipif(os.name == "nt", reason="fake sh script is POSIX-only")
def test_run_worker_end_to_end(tmp_path, monkeypatch, fake_claude):
    # Isolated profiles dir + a real profile on disk.
    monkeypatch.setattr("claudex.constants.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr("claudex.constants.SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr("claudex.constants.CLAUDEX_HOME", tmp_path)
    import importlib

    import claudex.core.profile

    importlib.reload(claudex.core.profile)
    from claudex.core.profile import ProfileManager

    ProfileManager().create("w")

    store = FleetStore(root=tmp_path / "fleet")
    job = Job(prompt="say hi", profile="w", status=JobStatus.ASSIGNED, cwd=str(tmp_path))
    store.save_job(job)

    from claudex.fleet.runner import run_worker

    rc = run_worker(job.id, store=store)
    assert rc == 0
    finished = store.load_job(job.id)
    assert finished.status == JobStatus.SUCCEEDED
    res = store.load_result(job.id)
    assert "hi from fake" in res.result_text
    assert res.cost_usd == 0.02
