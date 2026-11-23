"""Unit tests for device identity and remote fleet jobs."""

import base64

import pytest

from claudex import crypto
from claudex.core import device as device_mod
from claudex.core.device import DeviceIdentity, load_device, set_device_name
from claudex.fleet.remote import RemoteError, RemoteFleet


# ── device identity ──────────────────────────────────────────────────────────
def test_device_created_and_stable(tmp_path):
    p = tmp_path / "device.json"
    d1 = load_device(p)
    assert d1.device_id and d1.public_key and d1.private_key
    d2 = load_device(p)                       # reload: same identity
    assert d2.device_id == d1.device_id
    assert d2.private_key == d1.private_key
    assert d1.public()["device_id"] == d1.device_id
    assert "private_key" not in d1.public()   # never shared


def test_set_device_name(tmp_path):
    p = tmp_path / "device.json"
    load_device(p)
    d = set_device_name("work-mac", p)
    assert d.name == "work-mac"
    assert load_device(p).name == "work-mac"


# ── fake client + helpers ─────────────────────────────────────────────────────
def _device() -> DeviceIdentity:
    priv, pub = crypto.generate_keypair()
    return DeviceIdentity(device_id=pub[:12], name="t", public_key=pub, private_key=priv)


class _FakeClient:
    def __init__(self, host_pub=None):
        self._hosts = [{"target": "boxB", "public_key": host_pub}] if host_pub else []
        self.submitted = None
        self.completed = None
        self._claim = None
        self._job = None

    def list_fleet_hosts(self):
        return self._hosts

    def submit_fleet_job(self, target, submitter_pubkey, sealed_prompt, label=None, ttl_minutes=None):
        self.submitted = dict(target=target, submitter_pubkey=submitter_pubkey,
                              sealed_prompt=sealed_prompt, label=label)
        return {"job_id": "job123"}

    def register_fleet_host(self, target, public_key):
        self._hosts = [{"target": target, "public_key": public_key}]

    def claim_fleet_job(self, target):
        c, self._claim = self._claim, None
        return c

    def complete_fleet_job(self, job_id, status, sealed_result_b64=None, error=None):
        self.completed = dict(job_id=job_id, status=status,
                              sealed_result=sealed_result_b64, error=error)
        return {"status": status}

    def get_fleet_job(self, job_id):
        return self._job

    def list_fleet_jobs(self):
        return []


# ── submitter side ────────────────────────────────────────────────────────────
def test_submit_seals_prompt_to_host_key():
    host, submitter = _device(), _device()
    client = _FakeClient(host_pub=host.public_key)
    rf = RemoteFleet(client, submitter)
    job_id = rf.submit("boxB", "do the thing")
    assert job_id == "job123"
    # the server (and this test) only sees ciphertext; the host can unseal it
    sealed = base64.b64decode(client.submitted["sealed_prompt"])
    assert crypto.unseal(host.private_key, sealed).decode() == "do the thing"
    assert client.submitted["submitter_pubkey"] == submitter.public_key


def test_submit_unknown_host_raises():
    rf = RemoteFleet(_FakeClient(host_pub=None), _device())
    with pytest.raises(RemoteError):
        rf.submit("boxB", "x")


def test_poll_result_unseals_done():
    submitter = _device()
    client = _FakeClient()
    sealed = base64.b64encode(crypto.seal(submitter.public_key, b"42")).decode()
    client._job = {"status": "done", "sealed_result": sealed}
    rf = RemoteFleet(client, submitter)
    res = rf.poll_result("job123", timeout=1, interval=0.01)
    assert res.status == "done" and res.text == "42"


def test_poll_result_failed():
    client = _FakeClient()
    client._job = {"status": "failed", "error": "rate limited"}
    res = RemoteFleet(client, _device()).poll_result("j", timeout=1, interval=0.01)
    assert res.status == "failed" and res.error == "rate limited"


# ── host side ─────────────────────────────────────────────────────────────────
def test_serve_once_runs_and_seals_result_back():
    host, submitter = _device(), _device()
    client = _FakeClient()
    sealed_prompt = base64.b64encode(crypto.seal(host.public_key, b"what is 6*7?")).decode()
    client._claim = {"job_id": "j1", "submitter_pubkey": submitter.public_key,
                     "sealed_prompt": sealed_prompt}
    rf = RemoteFleet(client, host)

    seen = {}

    def run(prompt):
        seen["prompt"] = prompt
        return True, "42"

    assert rf.serve_once("boxB", run) == "j1"
    assert seen["prompt"] == "what is 6*7?"      # host unsealed the prompt
    assert client.completed["status"] == "done"
    # the result is sealed to the submitter — only they can read it
    back = crypto.unseal(submitter.private_key,
                         base64.b64decode(client.completed["sealed_result"])).decode()
    assert back == "42"


def test_serve_once_nothing_pending():
    rf = RemoteFleet(_FakeClient(), _device())
    assert rf.serve_once("boxB", lambda p: (True, "x")) is None


def test_serve_once_run_failure_marks_failed():
    host, submitter = _device(), _device()
    client = _FakeClient()
    client._claim = {"job_id": "j2", "submitter_pubkey": submitter.public_key,
                     "sealed_prompt": base64.b64encode(crypto.seal(host.public_key, b"x")).decode()}
    rf = RemoteFleet(client, host)

    def boom(prompt):
        raise RuntimeError("kaboom")

    assert rf.serve_once("boxB", boom) == "j2"
    assert client.completed["status"] == "failed"
    assert "kaboom" in client.completed["error"]
