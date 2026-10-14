"""Tests for the local reverse proxy (claudex.core.proxy)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from claudex.core import proxy


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
