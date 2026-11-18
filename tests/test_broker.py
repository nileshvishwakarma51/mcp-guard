"""Broker API security and decision-manager behaviour."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from conftest import ALLOW
from mcp_guard.config import TOKEN_HEADER

pytestmark = pytest.mark.asyncio

CALL = {"source": "claude-desktop", "server": "s", "method": "tools/call", "tool": "t", "arguments": {}}


async def test_rejects_missing_or_wrong_token(broker):
    url = broker.info.base_url + "/v1/decisions"
    async with aiohttp.ClientSession() as http:
        async with http.post(url, json=CALL) as r:
            assert r.status == 401
        async with http.post(url, json=CALL, headers={TOKEN_HEADER: "nope"}) as r:
            assert r.status == 401
        async with http.post(url, json=CALL, headers={TOKEN_HEADER: broker.info.token}) as r:
            assert r.status == 200 and (await r.json())["decision"] == ALLOW
    assert len(broker.ui.shown) == 1


async def test_rejects_browser_origin_and_foreign_host(broker):
    """A web page must not be able to drive the broker (CSRF / DNS rebinding)."""
    url = broker.info.base_url + "/v1/decisions"
    async with aiohttp.ClientSession() as http:
        headers = {TOKEN_HEADER: broker.info.token, "Origin": "https://evil.example"}
        async with http.post(url, json=CALL, headers=headers) as r:
            assert r.status == 403
        headers = {TOKEN_HEADER: broker.info.token, "Host": "evil.example"}
        async with http.post(url, json=CALL, headers=headers) as r:
            assert r.status == 403
    assert broker.ui.shown == []


async def test_rejects_malformed_bodies(broker):
    url = broker.info.base_url + "/v1/decisions"
    async with aiohttp.ClientSession() as http:
        for body in (b"not json", b"[]", json.dumps({"tool": "x"}).encode()):
            async with http.post(url, data=body, headers={TOKEN_HEADER: broker.info.token}) as r:
                assert r.status == 400


async def test_shutdown_blocks_everything_pending(broker):
    broker.ui.policy = lambda call: None
    tasks = [asyncio.create_task(broker.manager.decide(dict(CALL, tool=f"t{i}"))) for i in range(3)]
    await broker.ui.wait_open(3)
    broker.manager.block_all("MCP Guard is shutting down")
    results = await asyncio.gather(*tasks)
    assert all(r.decision == "block" and r.decided_by == "shutdown" for r in results)
    late = await broker.manager.decide(CALL)  # new requests after shutdown are refused
    assert late.decision == "block"


async def test_late_click_after_timeout_is_ignored(broker):
    broker.manager.timeout_s = 0.2
    broker.ui.policy = lambda call: None
    result = await broker.manager.decide(CALL)
    assert result.decided_by == "timeout"
    broker.manager.resolve("no-such-id", ALLOW)  # must not raise


async def test_audit_log_records_decisions(broker, guard_env):
    await broker.manager.decide(CALL)
    lines = [json.loads(l) for l in (guard_env / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == ["pending", "decision"]
    assert lines[1]["decision"] == "allow" and lines[1]["tool"] == "t"


async def test_second_broker_refuses_to_start(guard_env):
    """Single instance: a broker cannot start while the port is taken."""
    import socket
    import sys
    from conftest import free_port
    port = free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "mcp_guard", "broker", "--port", str(port),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == 1
    assert b"Is another MCP Guard broker running" in err
