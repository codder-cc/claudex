"""Remote fleet jobs — hand a prompt to another machine's fleet via a server.

Submitter (machine A) seals a prompt to the host's device key and posts it to a
relay server (codder.cc); the host (machine B) claims it, runs it on its fleet,
and seals the result back to the submitter's device key. The server stores only
ciphertext. Both machines log in to the same account (see claudex share auth).

This is the store-and-forward path (one prompt -> one result). Live streaming
into a remote session is the peer-link path (claudex.fleet.peer).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from claudex import crypto
from claudex.core.device import DeviceIdentity, load_device
from claudex.core.sharing_client import SharingAPIError, SharingClient, load_client


class RemoteError(Exception):
    pass


@dataclass
class RemoteResult:
    status: str                     # done | failed | timeout
    text: str = ""
    error: Optional[str] = None


# A host runner takes a prompt and returns (ok, result_text_or_error).
HostRunner = Callable[[str], Tuple[bool, str]]


class RemoteFleet:
    def __init__(self, client: SharingClient, device: DeviceIdentity):
        self._client = client
        self._device = device

    @classmethod
    def connect(cls, endpoint: str) -> "RemoteFleet":
        """Load a logged-in client for *endpoint* and this machine's identity."""
        return cls(load_client(endpoint), load_device())

    @property
    def device(self) -> DeviceIdentity:
        return self._device

    # ── submitter side ───────────────────────────────────────────────────────
    def host_pubkey(self, target: str) -> Optional[str]:
        for h in self._client.list_fleet_hosts():
            if h.get("target") == target:
                return h.get("public_key")
        return None

    def submit(self, target: str, prompt: str, label: Optional[str] = None,
               ttl_minutes: Optional[int] = None) -> str:
        """Seal *prompt* to the target host's key and submit it. Returns job_id."""
        pub = self.host_pubkey(target)
        if not pub:
            raise RemoteError(
                f"no host '{target}' registered — run `fleet remote serve "
                f"--target {target}` on that machine first")
        sealed = base64.b64encode(crypto.seal(pub, prompt.encode("utf-8"))).decode("ascii")
        resp = self._client.submit_fleet_job(
            target, self._device.public_key, sealed, label=label, ttl_minutes=ttl_minutes)
        return resp["job_id"]

    def poll_result(self, job_id: str, timeout: float = 1800.0,
                    interval: float = 2.0) -> RemoteResult:
        """Block until the job finishes, then unseal its result."""
        deadline = time.monotonic() + timeout
        while True:
            job = self._client.get_fleet_job(job_id)
            status = job.get("status")
            if status == "done":
                sealed = job.get("sealed_result")
                text = ""
                if sealed:
                    text = crypto.unseal(
                        self._device.private_key, base64.b64decode(sealed)).decode("utf-8")
                return RemoteResult(status="done", text=text)
            if status == "failed":
                return RemoteResult(status="failed", error=job.get("error") or "failed")
            if status == "expired":
                return RemoteResult(status="failed", error="job expired before it ran")
            if time.monotonic() > deadline:
                return RemoteResult(status="timeout", error="timed out waiting for result")
            time.sleep(interval)

    def hosts(self) -> list:
        return self._client.list_fleet_hosts()

    def jobs(self) -> list:
        return self._client.list_fleet_jobs()

    # ── host side ────────────────────────────────────────────────────────────
    def register(self, target: str) -> None:
        self._client.register_fleet_host(target, self._device.public_key)

    def serve_once(self, target: str, run: HostRunner) -> Optional[str]:
        """Claim at most one job for *target*, run it, seal the result back.

        Returns the job_id handled, or None when nothing was pending.
        """
        claim = self._client.claim_fleet_job(target)
        if not claim:
            return None
        job_id = claim["job_id"]
        submitter_pubkey = claim["submitter_pubkey"]
        try:
            prompt = crypto.unseal(
                self._device.private_key, base64.b64decode(claim["sealed_prompt"])
            ).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            self._client.complete_fleet_job(job_id, "failed", error=f"unseal failed: {e}")
            return job_id
        try:
            ok, text = run(prompt)
        except Exception as e:  # noqa: BLE001
            self._client.complete_fleet_job(job_id, "failed", error=str(e)[:2000])
            return job_id
        if ok:
            sealed = base64.b64encode(
                crypto.seal(submitter_pubkey, (text or "").encode("utf-8"))).decode("ascii")
            self._client.complete_fleet_job(job_id, "done", sealed_result_b64=sealed)
        else:
            self._client.complete_fleet_job(job_id, "failed", error=(text or "failed")[:2000])
        return job_id

    def serve_forever(self, target: str, run: HostRunner, interval: float = 3.0,
                      heartbeat_every: float = 30.0,
                      should_stop: Optional[Callable[[], bool]] = None,
                      on_job: Optional[Callable[[str, str], None]] = None) -> None:
        """Register the host and drain jobs until *should_stop* returns True."""
        self.register(target)
        last_beat = time.monotonic()
        while not (should_stop and should_stop()):
            now = time.monotonic()
            if now - last_beat >= heartbeat_every:
                try:
                    self.register(target)       # refresh last_seen / pubkey
                except SharingAPIError:
                    pass
                last_beat = now
            try:
                handled = self.serve_once(target, _wrap_on_job(run, on_job))
            except SharingAPIError:
                handled = None
            if handled is None:
                time.sleep(interval)


def _wrap_on_job(run: HostRunner, on_job) -> HostRunner:
    if on_job is None:
        return run

    def runner(prompt: str) -> Tuple[bool, str]:
        ok, text = run(prompt)
        try:
            on_job(prompt, text)
        except Exception:  # noqa: BLE001
            pass
        return ok, text

    return runner


def fleet_runner(profile: Optional[str], model: Optional[str] = None,
                 timeout_s: int = 1800) -> HostRunner:
    """A host runner that executes each remote prompt on the local fleet."""
    from claudex.fleet.engine import FleetEngine
    from claudex.fleet.models import JobStatus

    eng = FleetEngine()

    def run(prompt: str) -> Tuple[bool, str]:
        job = eng.dispatch(prompt, profile=profile, model=model, timeout_s=timeout_s)
        deadline = time.monotonic() + timeout_s + 60
        while True:
            cur = eng.status(job.id)
            if cur.is_terminal():
                break
            if time.monotonic() > deadline:
                return False, "local fleet job timed out"
            time.sleep(2)
        res = eng.result(job.id)
        if cur.status == JobStatus.SUCCEEDED and res:
            return True, res.result_text or ""
        err = (res.error if res else None) or f"job {cur.status.value}"
        return False, err

    return run
