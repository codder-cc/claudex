"""Tests for the OAuth login tunnel (URL out, code back over a peer channel)."""

import asyncio
import sys

import pytest

from claudex import crypto
from claudex.core import login_tunnel
from claudex.core.device import DeviceIdentity
from claudex.core.peer import PeerStore, handshake

# A real captured line from `claude auth login` has the URL twice (OSC-8 link).
REAL_OUTPUT = (
    "Opening browser to sign in…\n"
    "If the browser didn't open, visit: \x1b]8;;"
    "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=XYZ123"
    "\x1b\\https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=XYZ123"
    "\x1b]8;;\x1b\\\nPaste code here if prompted > "
)

FAKE_CLAUDE = (
    "import sys\n"
    "sys.stdout.write('Opening browser to sign in\\n')\n"
    "sys.stdout.write('visit: https://claude.com/cai/oauth/authorize?client_id=x&state=ABC123\\n')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline().strip()\n"
    "sys.exit(0 if code == 'THECODE' else 1)\n"
)


def _device(name: str) -> DeviceIdentity:
    priv, pub = crypto.generate_keypair()
    return DeviceIdentity(device_id=pub[:12], name=name, public_key=pub, private_key=priv)


# ── URL extraction ────────────────────────────────────────────────────────────
def test_extract_consent_url_clean():
    url = login_tunnel.extract_consent_url(REAL_OUTPUT)
    assert url == "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=XYZ123"


def test_extract_consent_url_none():
    assert login_tunnel.extract_consent_url("no url here") is None


# ── full relay over a loopback channel with a fake claude ─────────────────────
async def test_login_tunnel_relays_url_and_code(tmp_path, monkeypatch):
    fake = tmp_path / "fakeclaude.py"
    fake.write_text(FAKE_CLAUDE)
    monkeypatch.setattr(login_tunnel, "login_argv",
                        lambda email=None, console=False: [sys.executable, str(fake)])

    dev_a, dev_b = _device("A"), _device("B")
    sa, sb = PeerStore(tmp_path / "a.json"), PeerStore(tmp_path / "b.json")
    captured = {}
    helper_ok = {}

    async def on_conn(reader, writer):
        try:
            ch = await handshake(reader, writer, dev_b, sb, initiator=False)
            helper_ok["ok"] = await login_tunnel.run_helper(
                ch, open_url=lambda u: captured.__setitem__("url", u),
                get_code=lambda u: "THECODE")
        finally:
            writer.close()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ch = await login_tunnel.client_channel_lan(
            "127.0.0.1", port, dev_a, sa, expect_pubkey=dev_b.public_key)
        ok = await asyncio.wait_for(login_tunnel.run_initiator(ch, on_status=lambda m: None),
                                    timeout=15)
        assert ok is True                                   # claude got the right code
        assert "oauth/authorize" in captured.get("url", "")  # helper saw the URL
    finally:
        server.close()

    # give the helper's final recv a moment to settle
    await asyncio.sleep(0.1)
    assert helper_ok.get("ok") is True


async def test_initiator_fails_without_url(tmp_path, monkeypatch):
    # a fake claude that never prints a URL
    fake = tmp_path / "noclue.py"
    fake.write_text("import sys,time\nsys.stdout.write('nothing useful\\n')\nsys.stdout.flush()\ntime.sleep(2)\n")
    monkeypatch.setattr(login_tunnel, "login_argv",
                        lambda email=None, console=False: [sys.executable, str(fake)])
    dev_a, dev_b = _device("A"), _device("B")
    sa, sb = PeerStore(tmp_path / "a.json"), PeerStore(tmp_path / "b.json")

    async def on_conn(reader, writer):
        try:
            ch = await handshake(reader, writer, dev_b, sb, initiator=False)
            await login_tunnel.run_helper(ch, get_code=lambda u: "X")
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ch = await login_tunnel.client_channel_lan(
            "127.0.0.1", port, dev_a, sa, expect_pubkey=dev_b.public_key)
        ok = await asyncio.wait_for(
            login_tunnel.run_initiator(ch, on_status=lambda m: None, url_timeout=1.0),
            timeout=10)
        assert ok is False
    finally:
        server.close()
