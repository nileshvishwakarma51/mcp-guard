"""Shared fixtures.

``broker`` runs the *real* broker HTTP API and DecisionManager in the test's
event loop, with ``ScriptedUI`` standing in for the Tk popups: a policy
function decides what the "user" clicks and after how long.  Proxies under
test find it through the normal discovery file, exactly as in production.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
from aiohttp import web

from mcp_guard.broker import ALLOW, BLOCK, DecisionManager, build_app
from mcp_guard.config import BrokerInfo, new_token, write_broker_info

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# policy(call) -> (decision, delay_seconds) to auto-click, or None to leave it pending
Policy = Callable[[dict], "tuple[str, float] | None"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ScriptedUI:
    policy: Policy = lambda call: (ALLOW, 0.0)
    manager: DecisionManager | None = None
    shown: list[dict] = field(default_factory=list)
    open_ids: dict[str, dict] = field(default_factory=dict)
    closed: list[str] = field(default_factory=list)
    max_open: int = 0

    def show(self, pending_id: str, call: dict[str, Any]) -> None:
        self.shown.append(call)
        self.open_ids[pending_id] = call
        self.max_open = max(self.max_open, len(self.open_ids))
        action = self.policy(call)
        if action is not None:
            decision, delay = action
            asyncio.get_running_loop().call_later(delay, self.manager.resolve, pending_id, decision)

    def close(self, pending_id: str) -> None:
        self.open_ids.pop(pending_id, None)
        self.closed.append(pending_id)

    def click(self, pending_id: str, decision: str, remember: bool = False) -> None:
        self.manager.resolve(pending_id, decision, remember)

    async def wait_open(self, n: int = 1, timeout: float = 15) -> list[str]:
        async def _w():
            while len(self.open_ids) < n:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(_w(), timeout)
        return list(self.open_ids)


@dataclass
class RunningBroker:
    ui: ScriptedUI
    manager: DecisionManager
    info: BrokerInfo


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    home = tmp_path / "guard_home"
    monkeypatch.setenv("MCP_GUARD_HOME", str(home))
    monkeypatch.setenv("MCP_GUARD_NO_AUTOSTART", "1")
    monkeypatch.setenv("PYTHONPATH", ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return home


@pytest.fixture
async def broker(guard_env):
    ui = ScriptedUI()
    manager = DecisionManager(ui, timeout_s=30)
    ui.manager = manager
    port = free_port()
    token = new_token()
    runner = web.AppRunner(build_app(manager, token=token, port=port), handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    info = BrokerInfo("127.0.0.1", port, token, os.getpid())
    write_broker_info(info)
    yield RunningBroker(ui, manager, info)
    manager.block_all("test over")
    await runner.cleanup()


def by_tool(**decisions: str) -> Policy:
    """Policy helper: by_tool(get_weather="allow", delete_note="block")."""
    return lambda call: (decisions[call["tool"]], 0.0) if call.get("tool") in decisions else None


__all__ = ["ALLOW", "BLOCK", "PY", "ROOT", "by_tool", "free_port"]
