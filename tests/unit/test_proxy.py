"""Tests for the local reverse proxy (claudex.core.proxy)."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from claudex.core import proxy


def test_proxy_env_routes_and_disables_telemetry():
    e = proxy.proxy_env("http://127.0.0.1:1234")
    assert e["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1234"
    assert e["DISABLE_TELEMETRY"] == "1"


def test_status_running_for_live_liveness(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_FILE", tmp_path / ".proxy")
    srv = proxy.make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = srv.server_address[0], srv.server_address[1]
        (tmp_path / ".proxy").write_text(json.dumps(
            {"pid": os.getpid(), "host": host, "port": port, "url": f"http://{host}:{port}"}))
        st = proxy.status()
        assert st["running"] is True
        assert st["url"].endswith(str(port))
    finally:
        srv.shutdown()


def test_status_not_running_for_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_FILE", tmp_path / ".proxy")
    (tmp_path / ".proxy").write_text(json.dumps(
        {"pid": 999999, "host": "127.0.0.1", "port": 59998, "url": "http://127.0.0.1:59998"}))
    assert proxy.status()["running"] is False


def test_502_when_upstream_unreachable():
    """A dead upstream yields a clean 502 JSON error to the client (not a drop),
    so Claude Code's own error handling runs — and we never hammer the API."""
    srv = proxy.make_server(port=0, upstream_host="127.0.0.1", upstream_port=1)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = proxy.base_url_for(srv)
        req = urllib.request.Request(base + "/v1/messages", method="POST",
                                     data=b'{"x":1}',
                                     headers={"content-type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected a 502")
        except urllib.error.HTTPError as e:
            assert e.code == 502
            assert b"error" in e.read()
    finally:
        srv.shutdown()


def test_ensure_shared_reuses_running_without_spawning(monkeypatch):
    """If a daemon is already live, ensure_shared returns it and never spawns."""
    monkeypatch.setattr(proxy, "status",
                        lambda: {"running": True, "url": "http://127.0.0.1:40404"})
    spawned = {"n": 0}
    monkeypatch.setattr(proxy, "_spawn_daemon", lambda *a, **k: spawned.__setitem__("n", 1))
    assert proxy.ensure_shared() == "http://127.0.0.1:40404"
    assert spawned["n"] == 0


def test_stop_shared_cleans_file_for_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_FILE", tmp_path / ".proxy")
    (tmp_path / ".proxy").write_text(json.dumps(
        {"pid": 999999, "host": "127.0.0.1", "port": 1, "url": "http://x"}))
    assert proxy.stop_shared() is False     # dead pid → nothing killed
    assert not (tmp_path / ".proxy").exists()  # but stale file removed


def test_make_server_binds_free_port_and_base_url():
    srv = proxy.make_server(port=0)
    try:
        base = proxy.base_url_for(srv)
        assert base.startswith("http://127.0.0.1:")
        assert srv.server_address[1] > 0            # a real port was assigned
        assert srv.upstream_host == "api.anthropic.com"
    finally:
        srv.server_close()


def test_start_background_returns_running_url():
    srv, thread, base = proxy.start_background(port=0)
    try:
        assert thread.is_alive()
        assert base.startswith("http://127.0.0.1:")
    finally:
        srv.shutdown()


@pytest.mark.skipif(os.environ.get("CLAUDEX_NET_TESTS") != "1",
                    reason="network test; set CLAUDEX_NET_TESTS=1 to run")
def test_proxy_forwards_to_real_api():
    """An unauthenticated request should come back as a genuine Anthropic 401."""
    srv, thread, base = proxy.start_background(port=0)
    try:
        req = urllib.request.Request(
            base + "/v1/messages", method="POST",
            data=json.dumps({"model": "x", "max_tokens": 1, "messages": []}).encode(),
            headers={"content-type": "application/json", "anthropic-version": "2023-06-01"},
        )
        try:
            urllib.request.urlopen(req, timeout=30)
            raise AssertionError("expected a 401 from the upstream")
        except urllib.error.HTTPError as e:
            assert e.code == 401
            body = e.read().decode("utf-8", "replace")
            assert "authentication_error" in body  # proves it reached real api.anthropic.com
    finally:
        srv.shutdown()
