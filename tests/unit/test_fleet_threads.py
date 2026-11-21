"""Unit tests for conversation threads (mode A — resume pseudo-stream)."""

import pytest

from claudex.exceptions import FleetError, JobNotFoundError
from claudex.fleet import runner
from claudex.fleet.engine import FleetEngine
from claudex.fleet.models import Job, JobResult, JobStatus
from claudex.fleet.store import FleetStore


@pytest.fixture
def eng(tmp_path):
    return FleetEngine(store=FleetStore(root=tmp_path / "fleet"))


# ── worker re-invocation ──────────────────────────────────────────────────────
def test_claudex_command_prefers_current_interpreter():
    # When this interpreter can import claudex, the detached worker must run the
    # SAME code (python -m claudex.cli), not a possibly-older `claudex` on PATH —
    # otherwise it round-trips the job file and drops new fields like thread_id.
    import sys
    cmd = runner._claudex_command()
    assert cmd == [sys.executable, "-m", "claudex.cli"]


# ── _build_cmd ───────────────────────────────────────────────────────────────
def test_build_cmd_without_resume_has_no_resume_flag():
    cmd = runner._build_cmd(Job(prompt="hi"))
    assert "--resume" not in cmd


def test_build_cmd_with_resume_inserts_flag():
    cmd = runner._build_cmd(Job(prompt="hi", resume_session="sess-123"))
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-123"


# ── Job serialization ────────────────────────────────────────────────────────
def test_job_round_trips_thread_fields(tmp_path):
    store = FleetStore(root=tmp_path / "fleet")
    job = Job(prompt="x", thread_id="th_abc", resume_session="sess-9")
    store.save_job(job)
    loaded = store.load_job(job.id)
    assert loaded.thread_id == "th_abc"
    assert loaded.resume_session == "sess-9"


# ── continue_thread ──────────────────────────────────────────────────────────
def _finished_turn(store, thread_id, session_id, profile="work"):
    job = Job(prompt="turn 1", profile=profile, status=JobStatus.SUCCEEDED,
              thread_id=thread_id, cwd="/tmp")
    store.save_job(job)
    store.save_result(JobResult(job_id=job.id, status=JobStatus.SUCCEEDED,
                                result_text="ok", session_id=session_id))
    return job


def test_continue_thread_resumes_latest_session(eng):
    seed = _finished_turn(eng.store, "th_t1", "sess-abc", profile="work")
    nxt = eng.continue_thread("th_t1", "turn 2", auto_tick=False)
    assert nxt.thread_id == "th_t1"
    assert nxt.resume_session == "sess-abc"
    assert nxt.profile == "work"          # pinned to the seed's profile
    assert nxt.status == JobStatus.QUEUED  # not ticked, so not yet started
    assert nxt.id != seed.id


def test_continue_thread_by_job_id(eng):
    seed = _finished_turn(eng.store, "th_t2", "sess-def")
    nxt = eng.continue_thread(seed.id, "turn 2", auto_tick=False)
    assert nxt.resume_session == "sess-def"
    assert nxt.thread_id == "th_t2"


def test_continue_bare_job_backfills_new_thread(eng):
    # A plain dispatched job (no thread) becomes the seed of a fresh thread.
    seed = Job(prompt="solo", profile="work", status=JobStatus.SUCCEEDED, cwd="/tmp")
    eng.store.save_job(seed)
    eng.store.save_result(JobResult(job_id=seed.id, status=JobStatus.SUCCEEDED,
                                    session_id="sess-solo"))
    nxt = eng.continue_thread(seed.id, "follow up", auto_tick=False)
    assert nxt.thread_id is not None and nxt.thread_id.startswith("th_")
    # The seed was backfilled with the same thread id.
    assert eng.store.load_job(seed.id).thread_id == nxt.thread_id
    assert nxt.resume_session == "sess-solo"


def test_continue_thread_rejects_unfinished_turn(eng):
    job = Job(prompt="running", profile="work", status=JobStatus.RUNNING,
              thread_id="th_t3")
    eng.store.save_job(job)
    with pytest.raises(FleetError):
        eng.continue_thread("th_t3", "next", auto_tick=False)


def test_continue_thread_rejects_failed_turn(eng):
    job = Job(prompt="boom", profile="work", status=JobStatus.FAILED,
              thread_id="th_t4")
    eng.store.save_job(job)
    with pytest.raises(FleetError):
        eng.continue_thread("th_t4", "next", auto_tick=False)


def test_continue_thread_rejects_missing_session_id(eng):
    job = Job(prompt="no sid", profile="work", status=JobStatus.SUCCEEDED,
              thread_id="th_t5")
    eng.store.save_job(job)
    eng.store.save_result(JobResult(job_id=job.id, status=JobStatus.SUCCEEDED,
                                    session_id=None))
    with pytest.raises(FleetError):
        eng.continue_thread("th_t5", "next", auto_tick=False)


def test_continue_unknown_ref_raises(eng):
    with pytest.raises(JobNotFoundError):
        eng.continue_thread("th_nope", "x", auto_tick=False)


def test_start_thread_sets_thread_id(eng):
    job = eng.start_thread("hello", profile="work", auto_tick=False)
    assert job.thread_id is not None and job.thread_id.startswith("th_")
    assert job.resume_session is None


def test_thread_turns_orders_oldest_first(eng):
    t1 = _finished_turn(eng.store, "th_ord", "s1")
    t2 = eng.continue_thread("th_ord", "turn 2", auto_tick=False)
    turns = eng.thread_turns("th_ord")
    assert [t.id for t in turns] == [t1.id, t2.id]
