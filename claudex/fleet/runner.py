"""Process layer for fleet jobs.

Two entry points, because there is no daemon:

  spawn_detached(job)  — the *dispatcher* side. Launches a detached child process
                         `claudex fleet _run-worker <id>` that survives terminal
                         close, and returns its pid.

  run_worker(job_id)   — runs *inside* that detached process. It runs `claude -p`
                         in its own foreground, captures the result, classifies the
                         outcome, transitions the job, then pulls the next queued
                         job (self-propelling drain).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDE_BIN, CLAUDE_CONFIG_DIR_ENV, IS_WINDOWS
from claudex.core.auth import AuthManager
from claudex.core.profile import ProfileManager
from claudex.fleet.models import (
    FAIL_ERROR,
    FAIL_RATE_LIMIT,
    Job,
    JobResult,
    JobStatus,
    _now,
)
from claudex.fleet.scheduler import RateLimitClassifier, Scheduler
from claudex.fleet.store import FleetStore


def _claudex_command() -> list[str]:
    """How to re-invoke claudex for the detached worker."""
    exe = shutil.which("claudex")
    if exe:
        return [exe]
    return [sys.executable, "-m", "claudex.cli"]


def _detach_kwargs(log_fd) -> dict:
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        return {
            "creationflags": flags,
            "close_fds": True,
            "stdin": subprocess.DEVNULL,
            "stdout": log_fd,
            "stderr": subprocess.STDOUT,
        }
    return {
        "start_new_session": True,  # new session/process group; survives terminal close
        "stdin": subprocess.DEVNULL,
        "stdout": log_fd,
        "stderr": subprocess.STDOUT,
    }


def spawn_detached(store: FleetStore, job: Job) -> int:
    """Launch the detached worker process for *job*. Returns its pid."""
    log_path = store.log_path(job.id)
    log_fd = open(log_path, "ab")
    try:
        cmd = _claudex_command() + ["fleet", "_run-worker", job.id]
        proc = subprocess.Popen(cmd, **_detach_kwargs(log_fd))
    finally:
        log_fd.close()
    return proc.pid


def build_claude_env(auth: AuthManager, profile_name: str, config_dir: Path) -> dict:
    """Env for a worker targeting *profile_name*.

    Start from a clean copy of os.environ with any inherited CLAUDE_CONFIG_DIR
    stripped (so the MCP server running inside profile A never leaks A's config
    dir into a worker for profile B), then overlay the target profile's env.
    """
    env = {k: v for k, v in os.environ.items() if k != CLAUDE_CONFIG_DIR_ENV}
    env.update(auth.get_env_for_profile(profile_name, config_dir))
    return env


def _build_cmd(job: Job) -> list[str]:
    cmd = [CLAUDE_BIN, "-p", job.prompt, "--output-format", job.output_format]
    if job.model:
        cmd += ["--model", job.model]
    cmd += job.extra_args
    return cmd


def _parse_result(job: Job, stdout: str, status: JobStatus, error: Optional[str]) -> JobResult:
    result = JobResult(job_id=job.id, status=status, error=error)
    s = stdout.strip()
    payload = None
    if s.startswith("{"):
        try:
            payload = json.loads(s)
        except Exception:
            payload = None
    if payload:
        result.result_text = str(payload.get("result", "")) if not payload.get("is_error") else ""
        result.session_id = payload.get("session_id")
        result.cost_usd = payload.get("total_cost_usd")
        result.duration_ms = payload.get("duration_ms")
        result.num_turns = payload.get("num_turns")
        if payload.get("is_error") and not result.error:
            result.error = str(payload.get("result") or payload.get("error") or "claude reported an error")
    else:
        # text/stream-json: keep raw stdout as the result text
        result.result_text = stdout
    return result


def run_worker(job_id: str, store: Optional[FleetStore] = None) -> int:
    """Worker body (runs inside the detached process). Returns process exit code."""
    store = store or FleetStore()
    pm = ProfileManager()
    auth = AuthManager()
    classifier = RateLimitClassifier()

    job = store.load_job(job_id)
    if job is None:
        return 1

    # Claim ASSIGNED -> RUNNING (or QUEUED -> RUNNING if dispatched directly).
    claimed = store.compare_and_claim(
        job_id, JobStatus.ASSIGNED, JobStatus.RUNNING, pid=os.getpid(), bump_attempt=True
    )
    if claimed is None:
        claimed = store.compare_and_claim(
            job_id, JobStatus.QUEUED, JobStatus.RUNNING, pid=os.getpid(), bump_attempt=True
        )
    if claimed is None:
        return 0  # already taken / not runnable
    job = claimed
    job.started_at = _now()
    store.save_job(job)

    if not job.profile:
        return _fail_terminal(store, job, "no profile assigned")
    try:
        profile = pm.get(job.profile)
    except Exception as e:
        return _fail_terminal(store, job, f"profile load failed: {e}")

    env = build_claude_env(auth, job.profile, profile.config_dir)
    cwd = job.cwd if job.cwd and Path(job.cwd).is_dir() else os.getcwd()
    cmd = _build_cmd(job)

    timed_out = False
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=job.timeout_s
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = 124
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
    except FileNotFoundError:
        return _fail_terminal(store, job, "claude CLI not found in PATH")

    # Tee captured output into the log (Popen already wrote nothing useful here).
    try:
        with open(store.log_path(job.id), "a", encoding="utf-8") as lf:
            lf.write(stdout)
            if stderr:
                lf.write("\n[stderr]\n" + stderr)
    except Exception:
        pass

    job.exit_code = exit_code
    failure = classifier.classify(exit_code, stdout, stderr, timed_out=timed_out)

    scheduler = Scheduler(pm, auth, store)
    if failure is None:
        result = _parse_result(job, stdout, JobStatus.SUCCEEDED, None)
        store.save_result(result)
        scheduler.record_success(job.profile)
        _finalize(store, job, JobStatus.SUCCEEDED, None, None)
    elif failure == FAIL_RATE_LIMIT:
        retry_after = classifier.extract_retry_after(stdout, stderr)
        scheduler.record_rate_limit(job.profile, retry_after)
        result = _parse_result(job, stdout, JobStatus.RATE_LIMITED, "rate limited")
        store.save_result(result)
        _finalize(store, job, JobStatus.RATE_LIMITED, failure, "rate limited")
    else:
        msg = f"{failure}: exit {exit_code}"
        result = _parse_result(job, stdout, JobStatus.FAILED, msg)
        store.save_result(result)
        _finalize(store, job, JobStatus.FAILED, failure, msg)

    # Self-propelling drain: a finished worker frees a slot, so pull the next job.
    _drain(store)
    return 0


def _drain(store: FleetStore) -> None:
    """A finished worker frees a slot — pull the next queued job."""
    try:
        from claudex.fleet.engine import FleetEngine

        FleetEngine(store=store).tick(max_starts=1)
    except Exception:
        pass


def _fail_terminal(store: FleetStore, job: Job, msg: str) -> int:
    """Finalize a job as FAILED, persist a result so `fleet result` shows the
    error, and still drain the queue. Used for pre-launch terminal failures."""
    store.save_result(JobResult(job_id=job.id, status=JobStatus.FAILED, error=msg))
    _finalize(store, job, JobStatus.FAILED, FAIL_ERROR, msg)
    _drain(store)
    return 1


def _finalize(
    store: FleetStore,
    job: Job,
    status: JobStatus,
    failure_kind: Optional[str],
    error: Optional[str],
) -> None:
    job.status = status
    job.failure_kind = failure_kind
    job.finished_at = _now()
    job.pid = None
    store.save_job(job)
