"""End-to-end tests of the Claude Web path (Streamable HTTP reverse proxy)."""

from __future__ import annotations

import asyncio
import socket
import time

import aiohttp
import pytest
from aiohttp import web
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import ALLOW, BLOCK, PY, by_tool, free_port
from mcp_guard.broker_client import BrokerClient
from mcp_guard.config import DEFAULT_INTERCEPTED_METHODS
from mcp_guard.gate import Gate
from mcp_guard.http_proxy import HttpProxy

pytestmark = pytest.mark.asyncio
SECRET = "s3cret-path"


def _wait_port(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} did not open")


@pytest.fixture
async def upstream(guard_env):
    port = free_port()
    proc = await asyncio.create_subprocess_exec(PY, "-m", "demo_server", "--http", "--port", str(port),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await asyncio.to_thread(_wait_port, port)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    await proc.wait()


@pytest.fixture
async def proxy_url(upstream, broker):
    proxy = HttpProxy(upstream, Gate(BrokerClient(autostart=False)), name="remote-demo",
                      intercepted=DEFAULT_INTERCEPTED_METHODS, secret_path=SECRET)
    port = free_port()
    runner = web.AppRunner(proxy.build_app(), handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def _session_calls(url: str, calls):
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as s:
            await asyncio.wait_for(s.initialize(), 30)
            await s.list_tools()
            return await asyncio.wait_for(asyncio.gather(*(s.call_tool(n, a) for n, a in calls)), 60)


async def test_allow_and_block_over_http(proxy_url, broker):
    broker.ui.policy = by_tool(get_weather=ALLOW, send_email=BLOCK)
    ok, blocked = await _session_calls(f"{proxy_url}/{SECRET}/mcp", [
        ("get_weather", {"city": "Delhi"}),
        ("send_email", {"to": "x@y.z", "subject": "s", "body": "b"}),
    ])
    assert not ok.isError and "sunny in Delhi" in text_of(ok)
    assert blocked.isError and "Blocked by MCP Guard" in text_of(blocked)
    sources = {c["source"] for c in broker.ui.shown}
    assert sources == {"claude-web"}
    weather = next(c for c in broker.ui.shown if c["tool"] == "get_weather")
    assert weather["tool_info"]["annotations"]["readOnlyHint"] is True  # learned from (SSE) tools/list


async def test_concurrent_http_requests(proxy_url, broker):
    broker.ui.policy = lambda call: (ALLOW, 0.5) if call["arguments"]["text"] != "b" else (BLOCK, 0.1)
    results = await _session_calls(f"{proxy_url}/{SECRET}/mcp",
                                   [("slow_echo", {"text": t, "seconds": 0.2}) for t in "abc"])
    assert [r.isError for r in results] == [False, True, False]
    assert text_of(results[0]) == "a" and text_of(results[2]) == "c"
    assert broker.ui.max_open == 3


async def test_wrong_secret_path_is_404_and_never_reaches_upstream(proxy_url, broker):
    async with aiohttp.ClientSession() as http:
        for path in ("/mcp", "/wrong/mcp", f"/{SECRET}x/mcp"):
            async with http.post(proxy_url + path, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}) as r:
                assert r.status == 404
    assert broker.ui.shown == []


async def test_client_disconnect_withdraws_popup(proxy_url, broker):
    broker.ui.policy = lambda call: None
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "z"}}}
    async with aiohttp.ClientSession() as http:
        task = asyncio.create_task(http.post(f"{proxy_url}/{SECRET}/mcp", json=body,
                                             headers={"Accept": "application/json, text/event-stream"}))
        await broker.ui.wait_open(1)
        task.cancel()
    for _ in range(100):
        if not broker.ui.open_ids:
            break
        await asyncio.sleep(0.05)
    assert not broker.ui.open_ids


async def test_upstream_down_returns_502(broker, guard_env):
    proxy = HttpProxy(f"http://127.0.0.1:{free_port()}", Gate(BrokerClient(autostart=False)), name="x",
                      intercepted=DEFAULT_INTERCEPTED_METHODS, secret_path=None)
    port = free_port()
    runner = web.AppRunner(proxy.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"http://127.0.0.1:{port}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}) as r:
                assert r.status == 502
    finally:
        await runner.cleanup()


async def test_cancel_notification_withdraws_held_http_request(proxy_url, broker):
    """Streamable HTTP cancels with a separate POST; the held call must be withdrawn, never executed."""
    broker.ui.policy = lambda call: None
    url = f"{proxy_url}/{SECRET}/mcp"
    headers = {"Accept": "application/json, text/event-stream", "Mcp-Session-Id": "sess-1"}
    call = {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
            "params": {"name": "delete_note", "arguments": {"title": "x"}}}
    async with aiohttp.ClientSession() as http:
        async def held():
            async with http.post(url, json=call, headers=headers) as r:
                return await r.json()
        task = asyncio.create_task(held())
        await broker.ui.wait_open(1)
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 41}}
        async with http.post(url, json=cancel, headers=headers):
            pass
        reply = await asyncio.wait_for(task, 15)
    assert reply["id"] == 41 and reply["result"]["isError"] is True
    assert "cancelled by the client" in reply["result"]["content"][0]["text"]
    for _ in range(100):
        if not broker.ui.open_ids:
            break
        await asyncio.sleep(0.05)
    assert not broker.ui.open_ids, "popup must close when the client cancels"


async def test_http_batch_is_all_or_nothing(proxy_url, broker):
    broker.ui.policy = lambda call: (ALLOW if call["arguments"]["city"] == "ok" else BLOCK, 0)
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "ok"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "no"}}},
    ]
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{proxy_url}/{SECRET}/mcp", json=batch,
                             headers={"Accept": "application/json, text/event-stream"}) as r:
            replies = {m["id"]: m for m in await r.json()}
    assert replies[2]["result"]["isError"] is True and "denied by user" in replies[2]["result"]["content"][0]["text"]
    assert "another request in the same batch was blocked" in replies[1]["error"]["message"]
