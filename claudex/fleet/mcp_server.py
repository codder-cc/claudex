"""Local stdio MCP server exposing the fleet to an in-session Claude.

Registered into a profile via `claudex mcp setup --fleet`; Claude launches it over
stdio (`claudex mcp serve`). The server holds no long-lived workers — every tool
call advances the queue via FleetEngine.tick() and returns immediately, so it is
safe to start/stop with the Claude session.

Requires the optional `mcp` SDK: pip install 'claudex[fleet]'.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from claudex.fleet.engine import FleetEngine

mcp = FastMCP("claudex-fleet")


def _engine() -> FleetEngine:
    return FleetEngine()


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "status": job.status.value,
        "profile": job.profile,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "failure_kind": job.failure_kind,
        "is_orchestrator": job.is_orchestrator,
        "parent_id": job.parent_id,
        "child_ids": job.child_ids,
        "prompt": job.prompt,
    }


@mcp.tool()
def fleet_list_profiles() -> list[dict]:
    """List profiles (subscriptions) available to the fleet and their readiness.

    Use this to decide where to dispatch work: skip profiles that are in cooldown
    or have no usable auth.
    """
    eng = _engine()
    cooldowns = eng.store.load_cooldowns()
    now = eng.scheduler._clock()
    load: dict[str, int] = {}
    from claudex.fleet.models import JobStatus

    for st in (JobStatus.RUNNING, JobStatus.ASSIGNED):
        for j in eng.store.list_jobs(status=st):
            if j.profile:
                load[j.profile] = load.get(j.profile, 0) + 1

    out = []
    for p in eng.pm.list():
        try:
            status = eng.auth.get_status(p.name, p.config_dir)
            auth_type = status.auth_type
            expires_in = status.expires_in_human
        except Exception:
            auth_type, expires_in = "none", "—"
        cd = cooldowns.get(p.name)
        out.append({
            "name": p.name,
            "email": p.email,
            "auth_type": auth_type,
            "expires_in": expires_in,
            "in_cooldown": bool(cd and cd.in_cooldown(now)),
            "running_jobs": load.get(p.name, 0),
        })
    return out


@mcp.tool()
def fleet_dispatch(
    prompt: str,
    profile: Optional[str] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> dict:
    """Dispatch a headless `claude -p` agent job onto a profile (subscription).

    Leave `profile` empty to let the rate-limit-aware scheduler pick the
    least-loaded eligible subscription. Returns the job id; poll fleet_status and
    read fleet_result when it finishes.
    """
    eng = _engine()
    job = eng.dispatch(prompt, profile=profile, cwd=cwd, model=model, timeout_s=timeout_s)
    return _job_dict(job)


@mcp.tool()
def fleet_status(job_id: Optional[str] = None) -> dict | list[dict]:
    """Get one job's status, or a list of all jobs. Advances the queue first."""
    eng = _engine()
    if job_id:
        return _job_dict(eng.status(job_id))
    eng.tick()
    return [_job_dict(j) for j in eng.list_jobs()]


@mcp.tool()
def fleet_result(job_id: str) -> dict:
    """Get the result of a finished job (result text, cost, error, child rollup)."""
    eng = _engine()
    res = eng.result(job_id)
    if res is None:
        return {"job_id": job_id, "status": "pending", "result_text": ""}
    return res.to_dict()


@mcp.tool()
def fleet_cancel(job_id: str) -> dict:
    """Cancel a queued or running job."""
    eng = _engine()
    return _job_dict(eng.cancel(job_id))


@mcp.tool()
def fleet_fanout(
    task: str,
    subtasks: list[str],
    model: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> dict:
    """Fan one task out into parallel child jobs spread across subscriptions.

    Returns the orchestrator (parent) job id and its child ids. Poll fleet_status
    on the parent; once all children finish, fleet_result on the parent returns an
    aggregate with each child's output.
    """
    eng = _engine()
    parent = eng.fan_out(task, subtasks, model=model, timeout_s=timeout_s)
    return {"parent_id": parent.id, "child_ids": parent.child_ids}


def serve_stdio() -> None:
    mcp.run(transport="stdio")
