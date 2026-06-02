"""Unit tests for the fleet MCP server (requires the optional `mcp` SDK)."""

import pytest

pytest.importorskip("mcp", reason="fleet MCP server needs the 'mcp' SDK")


def test_tools_registered():
    import asyncio

    from claudex.fleet import mcp_server as m

    tools = asyncio.run(m.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "fleet_dispatch",
        "fleet_status",
        "fleet_result",
        "fleet_cancel",
        "fleet_fanout",
        "fleet_list_profiles",
    }


def test_dispatch_tool_uses_engine(monkeypatch):
    from claudex.fleet import mcp_server as m
    from claudex.fleet.models import Job, JobStatus

    captured = {}

    class FakeEngine:
        def dispatch(self, prompt, profile=None, cwd=None, model=None, timeout_s=None):
            captured["prompt"] = prompt
            captured["profile"] = profile
            return Job(prompt=prompt, profile=profile or "auto", status=JobStatus.ASSIGNED)

    monkeypatch.setattr(m, "_engine", lambda: FakeEngine())
    fn = getattr(m.fleet_dispatch, "fn", m.fleet_dispatch)
    out = fn("build the thing", profile="work")
    assert captured == {"prompt": "build the thing", "profile": "work"}
    assert out["status"] == "assigned"
    assert out["profile"] == "work"
