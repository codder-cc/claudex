"""claudex fleet — run headless `claude -p` agent jobs across multiple profiles.

Each profile is a separate Claude subscription; the fleet treats them as worker
pools, dispatching detached background jobs and routing around rate-limited or
expired accounts. State is file-backed under ~/.claudex/fleet/ (no daemon).
"""

from claudex.fleet.engine import FleetEngine
from claudex.fleet.models import Job, JobResult, JobStatus

__all__ = ["FleetEngine", "Job", "JobResult", "JobStatus"]
