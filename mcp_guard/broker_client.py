"""Client used by the proxies to ask the broker for a user decision.

The proxies are short-lived processes started by Claude (stdio) or by the
user (HTTP); the broker is one long-lived background process that owns the
UI.  This module finds the broker via the discovery file, starts it on demand
when it is not running, and performs the long-poll ``POST /v1/decisions``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

from .config import TOKEN_HEADER, BrokerInfo, read_broker_info
from .protocol import PendingCall

log = logging.getLogger(__name__)

AUTOSTART_WAIT_S = 15.0


class BrokerUnavailable(Exception):
    """The broker could not be reached (not running, crashed, refused)."""


class BrokerClient:
    def __init__(self, *, autostart: bool = True) -> None:
        self.autostart = autostart and os.environ.get("MCP_GUARD_NO_AUTOSTART") != "1"
        self._session: aiohttp.ClientSession | None = None
        self._start_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # No total timeout: a decision legitimately waits for a human.  The
            # broker enforces its own decision timeout and always answers.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _healthy(self, info: BrokerInfo) -> bool:
        try:
            async with self._http().get(f"{info.base_url}/v1/health",
                                        timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return data.get("pid") == info.pid
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    async def _ensure_broker(self) -> BrokerInfo:
        info = read_broker_info()
        if info is not None and await self._healthy(info):
            return info
        if not self.autostart:
            raise BrokerUnavailable("MCP Guard broker is not running")
        async with self._start_lock:
            info = read_broker_info()
            if info is not None and await self._healthy(info):
                return info
            spawn_broker_detached()
            deadline = time.monotonic() + AUTOSTART_WAIT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                info = read_broker_info()
                if info is not None and await self._healthy(info):
                    log.info("auto-started MCP Guard broker (pid %s)", info.pid)
                    return info
        raise BrokerUnavailable("MCP Guard broker could not be started")

    async def request_decision(self, call: PendingCall) -> tuple[bool, str]:
        """Block until the user decides.  Returns ``(allowed, reason)``."""
        info = await self._ensure_broker()
        try:
            async with self._http().post(
                f"{info.base_url}/v1/decisions",
                json=call.to_json(),
                headers={TOKEN_HEADER: info.token},
            ) as resp:
                if resp.status != 200:
                    raise BrokerUnavailable(f"broker answered HTTP {resp.status}")
                data = await resp.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise BrokerUnavailable(f"lost connection to broker: {exc}") from exc
        decision = data.get("decision")
        reason = str(data.get("reason") or "")
        if decision not in ("allow", "block"):
            raise BrokerUnavailable(f"malformed broker answer: {data!r}")
        return decision == "allow", reason


def spawn_broker_detached() -> None:
    """Start ``python -m mcp_guard broker`` fully detached from the caller.

    The broker must outlive the proxy that started it (Claude Desktop starts
    and stops proxies whenever it restarts servers) and must not hold the
    proxy's stdio pipes, or Claude would see garbage on the MCP channel.
    """
    exe = Path(sys.executable)
    if os.name == "nt":
        windowed = exe.with_name("pythonw.exe")
        if windowed.exists():
            exe = windowed
    cmd = [str(exe), "-m", "mcp_guard", "broker"]
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True)
    if os.name != "nt":
        kwargs["start_new_session"] = True
        log.info("starting broker: %s", cmd)
        subprocess.Popen(cmd, **kwargs)  # noqa: S603 - fixed argv, no shell
        return
    base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    # The launcher (Claude Desktop, a terminal, a CI runner) may run us inside a
    # Windows Job Object that kills the whole tree when it exits.  Ask to break
    # away so the broker survives; fall back if the job forbids breakaway.
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            log.info("starting broker: %s (flags=%#x)", cmd, flags)
            subprocess.Popen(cmd, creationflags=flags, **kwargs)  # noqa: S603
            return
        except OSError as exc:
            log.warning("broker spawn with flags %#x failed: %s", flags, exc)
    raise BrokerUnavailable("could not spawn the broker process")


CREATE_BREAKAWAY_FROM_JOB = 0x01000000
