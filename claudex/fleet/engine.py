"""FleetEngine — orchestration facade used by both the CLI and the MCP server.

No daemon: the queue is advanced by tick(), which is idempotent and safe to call
concurrently (all transitions go through FleetStore.compare_and_claim under a
single global lock). tick() is called by every state-changing CLI command, by
each finishing worker, and by MCP polls.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from claudex.constants import IS_WINDOWS
from claudex.core.auth import AuthManager
from claudex.core.profile import ProfileManager
from claudex.exceptions import JobNotFoundError
from claudex.fleet import runner
from claudex.fleet.models import (
    FAIL_ERROR,
    Job,
    JobResult,
    JobStatus,
    _now,
)
from claudex.fleet.scheduler import Scheduler
from claudex.fleet.store import FleetStore, is_pid_alive

# A RUNNING job whose pid is dead and that has no result is considered crashed.
# We also treat very old RUNNING jobs as dead to bound pid-reuse confusion.
_MAX_RUNNING_WALL_CLOCK = timedelta(hours=6)

# An ASSIGNED job whose worker never reached RUNNING within this window (and has
# no live pid) is treated as a failed launch.
_MAX_ASSIGN_WALL_CLOCK = timedelta(minutes=5)

# Default global cap on concurrently starting workers per tick / in flight.
DEFAULT_MAX_CONCURRENT = 8


@dataclass
class TickReport:
    reconciled: int = 0
    retried: int = 0
    started: int = 0
    skipped_no_profile: int = 0


class FleetEngine:
    def __init__(
        self,
        store: Optional[FleetStore] = None,
        profile_manager: Optional[ProfileManager] = None,
        auth_manager: Optional[AuthManager] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self.store = store or FleetStore()
        self.pm = profile_manager or ProfileManager()
        self.auth = auth_manager or AuthManager()
        self.scheduler = Scheduler(self.pm, self.auth, self.store, clock=clock)
        self._clock = clock
        self.max_concurrent = max_concurrent

    # ── public API ───────────────────────────────────────────────────────────
    def dispatch(
        self,
        prompt: str,
        profile: Optional[str] = None,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[int] = None,
        max_attempts: int = 3,
        output_format: str = "json",
        extra_args: Optional[list[str]] = None,
        parent_id: Optional[str] = None,
        auto_tick: bool = True,
    ) -> Job:
        job = Job(
            prompt=prompt,
            profile=profile,
            cwd=cwd or os.getcwd(),
            model=model,
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            output_format=output_format,
            extra_args=list(extra_args or []),
            parent_id=parent_id,
        )
        self.store.save_job(job)
        if auto_tick:
            self.tick()
            refreshed = self.store.load_job(job.id)
            if refreshed:
                job = refreshed
        return job

    def fan_out(
        self,
        task: str,
        subtasks: list[str],
        model: Optional[str] = None,
        timeout_s: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> Job:
        """Create an orchestrator parent + one child job per subtask."""
        parent = Job(
            prompt=task,
            is_orchestrator=True,
            status=JobStatus.RUNNING,
            cwd=cwd or os.getcwd(),
        )
        parent.started_at = _now()
        self.store.save_job(parent)
        for sub in subtasks:
            child = self.dispatch(
                prompt=sub,
                profile=None,
                cwd=cwd,
                model=model,
                timeout_s=timeout_s,
                parent_id=parent.id,
                auto_tick=False,
            )
            parent.child_ids.append(child.id)
        self.store.save_job(parent)
        self.tick()
        refreshed = self.store.load_job(parent.id)
        return refreshed or parent

    def status(self, job_id: str, tick_first: bool = True) -> Job:
        if tick_first:
            self.tick()
        resolved = self.store.resolve_job_id(job_id)
        job = self.store.load_job(resolved) if resolved else None
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def result(self, job_id: str) -> Optional[JobResult]:
        resolved = self.store.resolve_job_id(job_id)
        if not resolved:
            raise JobNotFoundError(job_id)
        return self.store.load_result(resolved)

    def list_jobs(self, **kwargs) -> list[Job]:
        return self.store.list_jobs(**kwargs)

    def cancel(self, job_id: str) -> Job:
        resolved = self.store.resolve_job_id(job_id)
        job = self.store.load_job(resolved) if resolved else None
        if job is None:
            raise JobNotFoundError(job_id)
        if job.is_terminal():
            return job
        if job.pid and is_pid_alive(job.pid):
            self._kill(job.pid)
        job.status = JobStatus.CANCELLED
        job.finished_at = _now()
        job.pid = None
        self.store.save_job(job)
        # Cascade cancel to children of an orchestrator so they stop consuming
        # subscriptions once the parent is cancelled.
        for cid in job.child_ids:
            child = self.store.load_job(cid)
            if child and not child.is_terminal():
                self.cancel(cid)
        return job

    def logs_path(self, job_id: str) -> Path:
        resolved = self.store.resolve_job_id(job_id)
        if not resolved:
            raise JobNotFoundError(job_id)
        return self.store.log_path(resolved)

    # ── the drain engine ──────────────────────────────────────────────────--
    def tick(self, max_starts: Optional[int] = None) -> TickReport:
        report = TickReport()
        self._reconcile(report)
        self._retry(report)
        self._schedule(report, max_starts)
        self._rollup_orchestrators()
        return report

    # ── tick stages ───────────────────────────────────────────────────────--
    def _reconcile(self, report: TickReport) -> None:
        now = self._clock()
        for job in self.store.list_jobs(status=JobStatus.RUNNING, include_orchestrators=False):
            if self.store.has_result(job.id):
                result = self.store.load_result(job.id)
                if result and not job.is_terminal():
                    job.status = result.status
                    job.finished_at = job.finished_at or _now()
                    job.pid = None
                    self.store.save_job(job)
                    report.reconciled += 1
                continue
            started = job.started_at or job.updated_at
            too_old = bool(started and (now - started) > _MAX_RUNNING_WALL_CLOCK)
            if not is_pid_alive(job.pid) or too_old:
                claimed = self.store.compare_and_claim(
                    job.id, JobStatus.RUNNING, JobStatus.FAILED
                )
                if claimed:
                    claimed.failure_kind = FAIL_ERROR
                    claimed.finished_at = _now()
                    claimed.pid = None
                    self.store.save_job(claimed)
                    report.reconciled += 1

        # ASSIGNED jobs whose detached worker died before reaching RUNNING (startup
        # crash, OOM) would otherwise sit forever — reconcile only scanned RUNNING.
        # A freshly-assigned job (pid not yet attached, recent) is left alone.
        for job in self.store.list_jobs(status=JobStatus.ASSIGNED, include_orchestrators=False):
            if self.store.has_result(job.id):
                result = self.store.load_result(job.id)
                if result:
                    job.status = result.status
                    job.finished_at = job.finished_at or _now()
                    job.pid = None
                    self.store.save_job(job)
                    report.reconciled += 1
                continue
            pid_dead = job.pid is not None and not is_pid_alive(job.pid)
            too_old = (now - job.updated_at) > _MAX_ASSIGN_WALL_CLOCK
            if pid_dead or too_old:
                claimed = self.store.compare_and_claim(
                    job.id, JobStatus.ASSIGNED, JobStatus.FAILED
                )
                if claimed:
                    claimed.failure_kind = FAIL_ERROR
                    claimed.finished_at = _now()
                    claimed.pid = None
                    self.store.save_job(claimed)
                    report.reconciled += 1

    def _retry(self, report: TickReport) -> None:
        candidates = self.store.list_jobs(
            status=JobStatus.RATE_LIMITED, include_orchestrators=False
        ) + self.store.list_jobs(status=JobStatus.FAILED, include_orchestrators=False)
        for job in candidates:
            if not job.is_retryable():
                continue
            if job.status == JobStatus.RATE_LIMITED and job.profile:
                # only retry once the profile's cooldown elapsed
                if self.scheduler.is_in_cooldown(job.profile):
                    continue
            expected = job.status
            claimed = self.store.compare_and_claim(job.id, expected, JobStatus.QUEUED)
            if claimed:
                claimed.pid = None
                claimed.failure_kind = None
                # rate-limited jobs may re-route to another profile
                if expected == JobStatus.RATE_LIMITED:
                    claimed.profile = None
                self.store.save_job(claimed)
                report.retried += 1

    def _schedule(self, report: TickReport, max_starts: Optional[int]) -> None:
        # Orchestrator parents sit in RUNNING but own no worker process — they must
        # NOT count against the worker concurrency budget, or enough concurrent
        # fan-outs would consume the whole budget and deadlock scheduling.
        running = len(
            self.store.list_jobs(status=JobStatus.RUNNING, include_orchestrators=False)
        ) + len(self.store.list_jobs(status=JobStatus.ASSIGNED, include_orchestrators=False))
        budget = self.max_concurrent - running
        if max_starts is not None:
            budget = min(budget, max_starts)
        if budget <= 0:
            return

        for job in self.store.list_jobs(status=JobStatus.QUEUED, include_orchestrators=False):
            if budget <= 0:
                break
            profile = job.profile or self.scheduler.choose_profile()
            if not profile:
                report.skipped_no_profile += 1
                continue
            # explicit profile in cooldown: skip this tick
            if job.profile and self.scheduler.is_in_cooldown(job.profile):
                continue
            self.scheduler.ensure_token_fresh(profile)
            claimed = self.store.compare_and_claim(
                job.id, JobStatus.QUEUED, JobStatus.ASSIGNED, profile=profile
            )
            if not claimed:
                continue
            try:
                pid = runner.spawn_detached(self.store, claimed)
                # Attach the pid under the lock and ONLY if the job is still
                # ASSIGNED — the detached worker may already have transitioned it
                # to RUNNING (or terminal) for a fast job. A blind save_job here
                # would clobber that transition back to ASSIGNED.
                self.store.attach_pid(claimed.id, pid)
                report.started += 1
                budget -= 1
            except Exception as e:
                claimed.status = JobStatus.FAILED
                claimed.failure_kind = FAIL_ERROR
                claimed.finished_at = _now()
                self.store.save_job(claimed)
                self.store.save_result(
                    JobResult(
                        job_id=claimed.id,
                        status=JobStatus.FAILED,
                        error=f"spawn failed: {e}",
                    )
                )

    def _rollup_orchestrators(self) -> None:
        for parent in self.store.list_jobs():
            if not parent.is_orchestrator or parent.is_terminal():
                continue
            children = [self.store.load_job(cid) for cid in parent.child_ids]
            children = [c for c in children if c is not None]
            if not children or not all(c.is_terminal() for c in children):
                continue
            succeeded = all(c.status == JobStatus.SUCCEEDED for c in children)
            child_summaries = []
            for c in children:
                cr = self.store.load_result(c.id)
                child_summaries.append(
                    {
                        "job_id": c.id,
                        "profile": c.profile,
                        "status": c.status.value,
                        "result_text": cr.result_text if cr else "",
                        "error": cr.error if cr else None,
                    }
                )
            parent.status = JobStatus.SUCCEEDED if succeeded else JobStatus.FAILED
            parent.finished_at = _now()
            self.store.save_job(parent)
            self.store.save_result(
                JobResult(
                    job_id=parent.id,
                    status=parent.status,
                    result_text="\n\n".join(
                        f"[{c['profile']}] {c['result_text']}" for c in child_summaries
                    ),
                    children=child_summaries,
                )
            )

    # ── helpers ───────────────────────────────────────────────────────────--
    def _kill(self, pid: int) -> None:
        if IS_WINDOWS:
            import subprocess

            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
            )
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass  # gone, or not ours to kill — cancellation proceeds regardless
