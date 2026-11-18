"""End-to-end tests of the Claude Desktop path.

The official MCP Python SDK client plays Claude Desktop and launches the
proxy exactly the way Claude Desktop would after ``mcp_guard install``.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conftest import ALLOW, BLOCK, PY, by_tool

pytestmark = pytest.mark.asyncio


def proxy_argv(server_cmd: list[str] | None = None) -> list[str]:
    server_cmd = server_cmd or [PY, "-m", "demo_server"]
    return ["-m", "mcp_guard", "stdio", "--name", "demo", "--no-autostart", "--", *server_cmd]


@asynccontextmanager
async def claude_session():
    params = StdioServerParameters(command=PY, args=proxy_argv(), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 30)
            yield session


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def test_allowed_call_reaches_server(broker):
    broker.ui.policy = by_tool(get_weather=ALLOW)
    async with claude_session() as s:
        tools = await s.list_tools()  # not intercepted: must pass straight through
        assert {"get_weather", "delete_note", "slow_echo"} <= {t.name for t in tools.tools}
        result = await asyncio.wait_for(s.call_tool("get_weather", {"city": "Pune"}), 30)
    assert not result.isError
    assert "sunny in Pune" in text_of(result)
    call = broker.ui.shown[0]
    assert (call["source"], call["server"], call["tool"]) == ("claude-desktop", "demo", "get_weather")
    assert call["arguments"] == {"city": "Pune"}
    # tool metadata learned from tools/list is shown in the popup
    assert "weather" in call["tool_info"]["description"].lower()
    assert call["tool_info"]["annotations"]["readOnlyHint"] is True


async def test_blocked_call_never_executes(broker, guard_env):
    broker.ui.policy = by_tool(write_note=ALLOW, delete_note=BLOCK)
    async with claude_session() as s:
        await s.list_tools()
        await asyncio.wait_for(s.call_tool("write_note", {"title": "keep me", "content": "x"}), 30)
        result = await asyncio.wait_for(s.call_tool("delete_note", {"title": "keep me"}), 30)
    assert result.isError
    assert "Blocked by MCP Guard" in text_of(result) and "denied by user" in text_of(result)
    assert (guard_env / "demo_notes" / "keep me.txt").exists(), "blocked delete must not run"
    assert broker.ui.shown[-1]["tool_info"]["annotations"]["destructiveHint"] is True


async def test_concurrent_requests_decided_independently(broker):
    # Answer in REVERSE order of arrival; allow even n, block odd n.
    def policy(call):
        n = int(call["arguments"]["text"])
        return (ALLOW if n % 2 == 0 else BLOCK), 0.3 * (5 - n)
    broker.ui.policy = policy
    async with claude_session() as s:
        results = await asyncio.wait_for(asyncio.gather(
            *(s.call_tool("slow_echo", {"text": str(n), "seconds": 0.2}) for n in range(5))), 60)
    for n, r in enumerate(results):
        if n % 2 == 0:
            assert not r.isError and text_of(r) == str(n)
        else:
            assert r.isError and "Blocked" in text_of(r)
    assert broker.ui.max_open == 5, "all five requests should have been pending at the same time"


async def test_other_traffic_flows_while_a_call_is_held(broker):
    broker.ui.policy = lambda call: None  # user has not clicked yet
    async with claude_session() as s:
        held = asyncio.create_task(s.call_tool("send_email", {"to": "a@b.c", "subject": "hi", "body": "x"}))
        [pid] = await broker.ui.wait_open(1)
        tools = await asyncio.wait_for(s.list_tools(), 10)  # must not be stuck behind the popup
        assert tools.tools
        assert not held.done()
        broker.ui.click(pid, ALLOW)
        result = await asyncio.wait_for(held, 30)
    assert "[simulated] Email" in text_of(result)


async def test_remember_choice_applies_to_waiting_and_future_calls(broker):
    broker.ui.policy = lambda call: None
    async with claude_session() as s:
        t1 = asyncio.create_task(s.call_tool("get_weather", {"city": "A"}))
        t2 = asyncio.create_task(s.call_tool("get_weather", {"city": "B"}))
        ids = await broker.ui.wait_open(2)
        broker.ui.click(ids[0], BLOCK, remember=True)
        r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), 30)
        r3 = await asyncio.wait_for(s.call_tool("get_weather", {"city": "C"}), 30)
    assert r1.isError and r2.isError and r3.isError
    assert len(broker.ui.shown) == 2, "third call must be decided by the rule without a popup"


async def test_timeout_blocks_request(broker):
    broker.manager.timeout_s = 1.0
    broker.ui.policy = lambda call: None
    async with claude_session() as s:
        result = await asyncio.wait_for(s.call_tool("get_weather", {"city": "X"}), 30)
    assert result.isError and "no decision within" in text_of(result)


async def test_fail_closed_when_broker_unreachable(guard_env):
    # No broker fixture and autostart disabled -> nobody can approve.
    async with claude_session() as s:
        result = await asyncio.wait_for(s.call_tool("get_weather", {"city": "X"}), 30)
    assert result.isError and "unavailable" in text_of(result)


# ------------------------------------------------------------------ raw-protocol edge cases


class RawClient:
    """Talks newline-delimited JSON-RPC to the proxy like Claude Desktop does."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    @classmethod
    async def start(cls, server_cmd: list[str] | None = None) -> "RawClient":
        proc = await asyncio.create_subprocess_exec(
            PY, *proxy_argv(server_cmd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024)
        return cls(proc)

    async def send(self, msg) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def recv(self, timeout: float = 20):
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        return json.loads(line)

    async def handshake(self) -> None:
        await self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "raw", "version": "1"}}})
        assert (await self.recv())["id"] == 0
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


async def test_cancelled_request_withdraws_popup_and_is_never_forwarded(broker):
    broker.ui.policy = lambda call: None
    c = await RawClient.start()
    try:
        await c.handshake()
        await c.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                      "params": {"name": "delete_note", "arguments": {"title": "x"}}})
        await broker.ui.wait_open(1)
        await c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
        for _ in range(100):
            if not broker.ui.open_ids:
                break
            await asyncio.sleep(0.05)
        assert not broker.ui.open_ids, "popup should close when Claude cancels"
        # Proxy still healthy, and no response for id 7 is ever sent.
        await c.send({"jsonrpc": "2.0", "id": 8, "method": "ping"})
        assert (await c.recv())["id"] == 8
    finally:
        c.proc.stdin.close()
        await asyncio.wait_for(c.proc.wait(), 15)


async def test_batch_with_mixed_decisions(broker):
    broker.ui.policy = lambda call: (ALLOW if call["arguments"]["city"] == "ok" else BLOCK, 0)
    c = await RawClient.start()
    try:
        await c.handshake()
        await c.send([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "ok"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "no"}}},
        ])
        got = {}
        while len(got) < 2:
            msg = await c.recv()
            for m in msg if isinstance(msg, list) else [msg]:
                got[m["id"]] = m
        assert got[1]["result"]["isError"] is False
        assert got[2]["result"]["isError"] is True
    finally:
        c.proc.stdin.close()
        await asyncio.wait_for(c.proc.wait(), 15)


async def test_proxy_exits_when_claude_closes_stdin(broker):
    c = await RawClient.start()
    await c.handshake()
    c.proc.stdin.close()
    assert await asyncio.wait_for(c.proc.wait(), 15) == 0


async def test_proxy_reports_server_crash_exit_code(broker):
    c = await RawClient.start([PY, "-c", "import sys; sys.exit(3)"])
    assert await asyncio.wait_for(c.proc.wait(), 15) == 3


async def test_missing_server_binary(broker):
    c = await RawClient.start(["definitely-not-a-real-mcp-server-binary"])
    assert await asyncio.wait_for(c.proc.wait(), 15) == 127


async def test_duplicate_pending_request_id_is_rejected(broker):
    broker.ui.policy = lambda call: None
    c = await RawClient.start()
    try:
        await c.handshake()
        msg = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": "get_weather", "arguments": {"city": "a"}}}
        await c.send(msg)
        await broker.ui.wait_open(1)
        await c.send(msg)  # same id while the first is still pending
        reply = await c.recv()
        assert reply["id"] == 5 and reply["error"]["code"] == -32600
        assert len(broker.ui.open_ids) == 1
    finally:
        c.proc.stdin.close()
        await asyncio.wait_for(c.proc.wait(), 15)


async def test_held_request_gets_error_when_server_crashes(broker):
    broker.ui.policy = lambda call: None
    # A "server" that reads nothing and dies after 3 seconds.
    c = await RawClient.start([PY, "-c", "import time, sys; time.sleep(3); sys.exit(4)"])
    await c.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                  "params": {"name": "anything", "arguments": {}}})
    await broker.ui.wait_open(1)
    reply = await c.recv(timeout=20)
    assert reply["id"] == 9 and "exited before the request ran" in reply["error"]["message"]
    assert await asyncio.wait_for(c.proc.wait(), 15) == 4
    for _ in range(100):
        if not broker.ui.open_ids:
            break
        await asyncio.sleep(0.05)
    assert not broker.ui.open_ids, "popup for the dead server's request must close"


async def test_large_message_passes_through(broker):
    broker.ui.policy = lambda call: (ALLOW, 0)
    big = "x" * 2_000_000  # 2 MB argument, well above typical pipe buffers
    c = await RawClient.start()
    try:
        await c.handshake()
        await c.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "slow_echo", "arguments": {"text": big, "seconds": 0}}})
        reply = await c.recv(timeout=60)
        assert reply["id"] == 3 and reply["result"]["content"][0]["text"] == big
        assert len(broker.ui.shown[0]["arguments"]["text"]) == len(big)  # full data reached the broker
    finally:
        c.proc.stdin.close()
        await asyncio.wait_for(c.proc.wait(), 15)



async def test_forward_to_dead_server_answers_with_error_instead_of_dying():
    """Unit-level: a write failure to the server must become an error reply, not crash the pump."""
    from mcp_guard.stdio_proxy import StdioProxy

    class DeadStdin:
        def write(self, data):
            raise BrokenPipeError("pipe closed")
        async def drain(self):
            pass

    class DeadChild:
        stdin = DeadStdin()
        returncode = None

    proxy = StdioProxy("t", ["unused"], gate=None, intercepted=frozenset({"tools/call"}))
    proxy._child = DeadChild()
    await proxy._handle_message({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    await proxy._handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, b"{}")
    replies = []
    while not proxy._outbox.empty():
        replies.append(json.loads(proxy._outbox.get()))
    assert len(replies) == 1  # the request is answered, the notification needs no reply
    assert replies[0]["id"] == 1 and "MCP server unavailable" in replies[0]["error"]["message"]
