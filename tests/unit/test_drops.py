"""Auth drops in claudex: remote `claude /login` hand-off through the sharing server."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from claudex.core import sharing_client as client_mod
from claudex.core.sharing_client import SharingAPIError
from claudex.crypto import decode_drop_token, encode_drop_token, generate_keypair, seal, unseal
from claudex.exceptions import ClaudexError


def test_sealed_box_and_token_roundtrip():
    priv, pub = generate_keypair()
    blob = seal(pub, b"x" * 500)
    assert unseal(priv, blob) == b"x" * 500
    with pytest.raises(ValueError):
        unseal(generate_keypair()[0], blob)
    token = encode_drop_token("https://codder.cc/", "d", "w", pub)
    assert decode_drop_token(token)["e"] == "https://codder.cc" and decode_drop_token(token)["b"] == "claude"
    with pytest.raises(ValueError):
        decode_drop_token("cx_nope")


def test_token_is_compatible_with_aimux_layout():
    """aimux and claudex share the `axd_` format: same JSON keys, same seal."""
    _, pub = generate_keypair()
    token = encode_drop_token("https://codder.cc", "id", "wt", pub, "claude")
    payload = json.loads(base64.urlsafe_b64decode(token[4:] + "=" * ((-len(token[4:])) % 4)))
    assert set(payload) == {"v", "e", "d", "w", "k", "b"} and payload["v"] == 1


class FakeServer:
    def __init__(self):
        self.drops = {}

    def http(self, method, url, data=None, headers=None):
        headers = headers or {}
        path = url.split("/api/v1/claudex", 1)[1]
        if path == "/drops" and method == "POST":
            assert headers.get("Authorization")
            did = str(uuid.uuid4())
            self.drops[did] = {"token": "wt-" + did[:6], "data": None,
                               "expires": datetime.now(timezone.utc) + timedelta(minutes=data["ttl_minutes"])}
            return {"drop_id": did, "write_token": self.drops[did]["token"], "expires_at": "x"}
        did = path.rsplit("/", 1)[-1]
        drop = self.drops.get(did)
        if method == "PUT":
            assert "Authorization" not in headers
            if drop is None:
                raise SharingAPIError(404, "not found")
            if drop["token"] is None or drop["expires"] < datetime.now(timezone.utc):
                raise SharingAPIError(410, "gone")
            if headers.get("X-Drop-Token") != drop["token"]:
                raise SharingAPIError(401, "bad token")
            drop["data"] = data["encrypted_data"]; drop["token"] = None
            return {"status": "filled"}
        if method == "GET":
            if drop is None:
                raise SharingAPIError(404, "not found")
            if drop["data"] is None:
                if drop["expires"] < datetime.now(timezone.utc):
                    raise SharingAPIError(410, "expired")
                return {}
            return {"encrypted_data": drop["data"], "backend": "claude"}
        if method == "DELETE":
            self.drops.pop(did, None)
            return {}
        raise AssertionError(method + url)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import claudex.constants as C
    for name, value in {"CLAUDEX_HOME": tmp_path, "PROFILES_DIR": tmp_path / "profiles",
                        "SHARED_DIR": tmp_path / "shared", "ACTIVE_PROFILE_FILE": tmp_path / ".active",
                        "CURRENT_ENV_BASH": tmp_path / ".env", "CURRENT_ENV_PWSH": tmp_path / ".env.ps1",
                        "GLOBAL_CONFIG_FILE": tmp_path / "config.toml", "IS_MACOS": False}.items():
        monkeypatch.setattr(C, name, value)
    import importlib
    import claudex.core.profile as prof
    importlib.reload(prof)
    # credential backend: in-memory
    store = {}
    class Mem:
        def store(self, p, k, v): store[(p, k)] = v
        def retrieve(self, p, k): return store.get((p, k))
        def delete(self, p, k): store.pop((p, k), None)
    monkeypatch.setattr("claudex.core.auth.get_credential_backend", lambda: Mem())
    fake = FakeServer()
    monkeypatch.setattr(client_mod, "_http", fake.http)
    monkeypatch.setattr(client_mod, "load_client", lambda ep: client_mod.SharingClient(ep, "jwt-b"))
    import claudex.core.drops as drops
    monkeypatch.setattr(drops, "load_client", client_mod.load_client)
    return tmp_path, fake, store


def _fake_login(env):
    from pathlib import Path
    d = Path(env["CLAUDE_CONFIG_DIR"])
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-remote", "refreshToken": "sk-ant-ort01-r", "expiresAt": 1893456000000,
        "scopes": ["user:inference"], "subscriptionType": "max"}}))
    (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@x.y", "accountUuid": "u"},
                                                "hasCompletedOnboarding": True, "projects": {"/a": {}}}))


def test_remote_login_handoff(home, monkeypatch):
    tmp_path, fake, store = home
    from claudex.core.drops import fulfill_drop, request_drop, wait_for_drop
    from claudex.core.profile import ProfileManager

    req = request_drop("work", "https://codder.cc", ttl_minutes=10)
    pm = ProfileManager()
    assert pm.exists("work")
    cfg = pm.get("work").config_dir
    (cfg / ".claude.json").write_text(json.dumps({"projects": {"/repo": {"hasTrustDialogAccepted": True}}}))

    assert fulfill_drop(req.token, login_runner=_fake_login) == "claude"
    drop = fake.drops[req.drop_id]
    assert drop["token"] is None and b"sk-ant-oat01" not in base64.b64decode(drop["data"])
    assert not (tmp_path / ".drops" / req.drop_id).exists()
    assert not any(k[0].startswith("drop-") for k in store)            # temp creds purged

    profile = wait_for_drop(req, timeout_s=5, poll_s=0)
    assert json.loads((cfg / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-remote"
    merged = json.loads((cfg / ".claude.json").read_text())
    assert merged["oauthAccount"]["emailAddress"] == "me@x.y" and merged["projects"] == {"/repo": {"hasTrustDialogAccepted": True}}
    assert profile.auth_type == "oauth" and profile.email == "me@x.y"
    assert store[("work", "oauth_token")] == "sk-ant-oat01-remote"
    assert req.drop_id not in fake.drops


def test_fulfill_refuses_other_backends_and_empty_login(home):
    tmp_path, fake, _ = home
    from claudex.core.drops import fulfill_drop, request_drop

    _, pub = generate_keypair()
    with pytest.raises(ClaudexError, match="kimi"):
        fulfill_drop(encode_drop_token("https://codder.cc", "d", "w", pub, "kimi"), login_runner=lambda env: None)
    req = request_drop("p", "https://codder.cc")
    with pytest.raises(ClaudexError, match="did not produce"):
        fulfill_drop(req.token, login_runner=lambda env: None)
    assert fake.drops[req.drop_id]["data"] is None


def test_wait_reports_timeout_and_expiry(home):
    tmp_path, fake, _ = home
    from claudex.core.drops import request_drop, wait_for_drop

    req = request_drop("p", "https://codder.cc")
    with pytest.raises(ClaudexError, match="Timed out"):
        wait_for_drop(req, timeout_s=0, poll_s=0)
    fake.drops[req.drop_id]["expires"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(ClaudexError, match="expired"):
        wait_for_drop(req, timeout_s=5, poll_s=0)
