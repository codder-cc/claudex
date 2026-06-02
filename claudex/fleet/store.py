"""File-backed fleet state store.

Design goal: correct concurrent access from many short-lived processes with NO
daemon. Each job is its own file (minimal write contention); cross-cutting
transitions take a short OS advisory lock. Writes are atomic (os.replace).

Layout under FLEET_DIR (~/.claudex/fleet/):
    jobs/<id>.json        one Job record per file
    logs/<id>.log         combined stdout+stderr of the claude -p process
    results/<id>.json     raw JobResult written by the worker on exit
    cooldowns.json        per-profile cooldown map
    fleet.lock            global advisory lock for multi-record transitions
    refresh-<name>.lock   per-profile lock guarding token refresh
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from claudex.constants import FLEET_DIR, IS_WINDOWS
from claudex.fleet.models import Job, JobResult, JobStatus, ProfileCooldown


# ── pid liveness ─────────────────────────────────────────────────────────────
def is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)  # type: ignore[attr-defined]
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    return True


# ── cross-platform advisory file lock ────────────────────────────────────────
@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if IS_WINDOWS:
            import msvcrt

            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                    break
                except OSError:
                    import time

                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                except OSError:
                    pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on same filesystem, Windows-safe


class FleetStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or FLEET_DIR
        self.jobs_dir = self.root / "jobs"
        self.logs_dir = self.root / "logs"
        self.results_dir = self.root / "results"
        for d in (self.jobs_dir, self.logs_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── paths ────────────────────────────────────────────────────────────────
    def job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        return self.logs_dir / f"{job_id}.log"

    def result_path(self, job_id: str) -> Path:
        return self.results_dir / f"{job_id}.json"

    @property
    def lock_path(self) -> Path:
        return self.root / "fleet.lock"

    def refresh_lock_path(self, profile: str) -> Path:
        return self.root / f"refresh-{profile}.lock"

    @property
    def cooldowns_path(self) -> Path:
        return self.root / "cooldowns.json"

    # ── locking ──────────────────────────────────────────────────────────────
    @contextmanager
    def lock(self) -> Iterator[None]:
        with _file_lock(self.lock_path):
            yield

    @contextmanager
    def refresh_lock(self, profile: str) -> Iterator[None]:
        with _file_lock(self.refresh_lock_path(profile)):
            yield

    # ── jobs ───────────────────────────────────────────────────────────────--
    def save_job(self, job: Job) -> None:
        from claudex.fleet.models import _now

        job.updated_at = _now()
        _atomic_write(self.job_path(job.id), json.dumps(job.to_dict(), indent=2))

    def load_job(self, job_id: str) -> Optional[Job]:
        path = self.job_path(job_id)
        if not path.exists():
            return None
        return Job.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def resolve_job_id(self, prefix: str) -> Optional[str]:
        """Resolve a full id or unique prefix to a job id."""
        if self.job_path(prefix).exists():
            return prefix
        matches = [p.stem for p in self.jobs_dir.glob(f"{prefix}*.json")]
        return matches[0] if len(matches) == 1 else None

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        profile: Optional[str] = None,
        parent_id: Optional[str] = None,
        include_orchestrators: bool = True,
    ) -> list[Job]:
        jobs = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                job = Job.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if status is not None and job.status != status:
                continue
            if profile is not None and job.profile != profile:
                continue
            if parent_id is not None and job.parent_id != parent_id:
                continue
            if not include_orchestrators and job.is_orchestrator:
                continue
            jobs.append(job)
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    def delete_job(self, job_id: str) -> None:
        self.job_path(job_id).unlink(missing_ok=True)
        self.log_path(job_id).unlink(missing_ok=True)
        self.result_path(job_id).unlink(missing_ok=True)

    def compare_and_claim(
        self,
        job_id: str,
        expected: JobStatus,
        new_status: JobStatus,
        profile: Optional[str] = None,
        pid: Optional[int] = None,
        bump_attempt: bool = False,
    ) -> Optional[Job]:
        """Atomically transition a job only if it is still in *expected* status.

        Returns the updated Job on success, None if another process already moved
        it. The single global lock guards the read-modify-write so two reconcilers
        cannot both start the same queued job.
        """
        with self.lock():
            job = self.load_job(job_id)
            if job is None or job.status != expected:
                return None
            job.status = new_status
            if profile is not None:
                job.profile = profile
            if pid is not None:
                job.pid = pid
            if bump_attempt:
                job.attempts += 1
            self.save_job(job)
            return job

    # ── results ────────────────────────────────────────────────────────────--
    def save_result(self, result: JobResult) -> None:
        _atomic_write(
            self.result_path(result.job_id), json.dumps(result.to_dict(), indent=2)
        )

    def load_result(self, job_id: str) -> Optional[JobResult]:
        path = self.result_path(job_id)
        if not path.exists():
            return None
        return JobResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def has_result(self, job_id: str) -> bool:
        return self.result_path(job_id).exists()

    # ── cooldowns ──────────────────────────────────────────────────────────--
    def load_cooldowns(self) -> dict[str, ProfileCooldown]:
        path = self.cooldowns_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {k: ProfileCooldown.from_dict(v) for k, v in data.items()}

    def save_cooldowns(self, cooldowns: dict[str, ProfileCooldown]) -> None:
        payload = {k: v.to_dict() for k, v in cooldowns.items()}
        _atomic_write(self.cooldowns_path, json.dumps(payload, indent=2))
