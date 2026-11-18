"""The broker: one background process that owns all pending decisions and the UI.

Threading model
---------------
* The **main thread** runs the Tk event loop (Tk must live on one thread, and
  on macOS it must be the main one).
* A **worker thread** runs an asyncio loop with a small aiohttp server bound to
  127.0.0.1.  Proxies long-poll ``POST /v1/decisions``; each poll is one
  pending request, so any number of simultaneous requests are handled
  independently.
* The two sides talk only through thread-safe hand-offs: ``UIBackend.show`` /
  ``close`` (queued into Tk) and ``DecisionManager.resolve_threadsafe``
  (scheduled onto the asyncio loop).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from aiohttp import web

from .audit import AuditLog
from .config import (DEFAULT_BROKER_HOST, DEFAULT_BROKER_PORT, DEFAULT_DECISION_TIMEOUT_S, TOKEN_HEADER,
                     BrokerInfo, new_token, remove_broker_info, write_broker_info)

log = logging.getLogger(__name__)

ALLOW = "allow"
BLOCK = "block"

MAX_REQUEST_BYTES = 8 * 1024 * 1024


class UIBackend(Protocol):
    """What the decision manager needs from a user interface.  Must be thread-safe."""

    def show(self, pending_id: str, call: dict[str, Any]) -> None: ...
    def close(self, pending_id: str) -> None: ...


@dataclass
class DecisionResult:
    decision: str
    reason: str
    decided_by: str = "user"  # user | rule | timeout | shutdown | error

    def to_json(self) -> dict[str, str]:
        return {"decision": self.decision, "reason": self.reason}


def rule_key(call: dict[str, Any]) -> tuple[str, str, str]:
    return (str(call.get("server")), str(call.get("method")), str(call.get("tool")))


class DecisionManager:
    """Tracks pending requests and turns UI clicks / timeouts into results.

    Lives on the asyncio loop.  Every public coroutine and method except
    ``resolve_threadsafe`` must be called from that loop.
    """

    def __init__(self, ui: UIBackend, *, timeout_s: float = DEFAULT_DECISION_TIMEOUT_S,
                 audit: AuditLog | None = None) -> None:
        self.ui = ui
        self.timeout_s = timeout_s
        self.audit = audit or AuditLog()
        self._pending: dict[str, tuple[asyncio.Future[DecisionResult], dict[str, Any]]] = {}
        # Session-scoped "remember my choice" rules: (server, method, tool) -> decision.
        self._rules: dict[tuple[str, str, str], str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_summary(self) -> list[dict[str, Any]]:
        return [{"id": pid, "server": c.get("server"), "tool": c.get("tool"), "method": c.get("method"),
                 "source": c.get("source")} for pid, (_, c) in self._pending.items()]

    async def decide(self, call: dict[str, Any]) -> DecisionResult:
        self._loop = asyncio.get_running_loop()
        if self._closing:
            return DecisionResult(BLOCK, "MCP Guard is shutting down", "shutdown")

        remembered = self._rules.get(rule_key(call))
        if remembered is not None:
            result = DecisionResult(remembered, f"remembered {remembered} rule for this tool", "rule")
            self.audit.record("decision", decided_by="rule", **result.to_json(), **_audit_fields(call))
            return result

        pending_id = uuid.uuid4().hex
        fut: asyncio.Future[DecisionResult] = self._loop.create_future()
        call = {**call, "timeout_s": self.timeout_s, "received_at": time.time()}
        self._pending[pending_id] = (fut, call)
        self.audit.record("pending", pending_id=pending_id, **_audit_fields(call))
        try:
            self.ui.show(pending_id, call)
        except Exception:
            log.exception("UI failed to show request; blocking it")
            self._pending.pop(pending_id, None)
            return DecisionResult(BLOCK, "confirmation dialog could not be displayed", "error")

        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            result = DecisionResult(BLOCK, f"no decision within {self.timeout_s:.0f}s", "timeout")
        except asyncio.CancelledError:
            # The proxy hung up (Claude cancelled the request or exited).
            self.audit.record("cancelled", pending_id=pending_id, **_audit_fields(call))
            raise
        finally:
            self._pending.pop(pending_id, None)
            self.ui.close(pending_id)
        self.audit.record("decision", pending_id=pending_id, decided_by=result.decided_by,
                          **result.to_json(), **_audit_fields(call))
        return result

    def resolve(self, pending_id: str, decision: str, remember: bool = False) -> None:
        if decision not in (ALLOW, BLOCK):
            raise ValueError(decision)
        entry = self._pending.get(pending_id)
        if entry is None:
            return  # already timed out / cancelled; click arrived late
        fut, call = entry
        reason = "approved by user" if decision == ALLOW else "denied by user"
        if remember:
            key = rule_key(call)
            self._rules[key] = decision
            reason += " (remembered for this tool)"
            # Apply the new rule to identical requests that are already waiting.
            for other_id, (other_fut, other_call) in list(self._pending.items()):
                if other_id != pending_id and rule_key(other_call) == key and not other_fut.done():
                    other_fut.set_result(DecisionResult(decision, f"remembered {decision} rule for this tool", "rule"))
        if not fut.done():
            fut.set_result(DecisionResult(decision, reason))

    def resolve_threadsafe(self, pending_id: str, decision: str, remember: bool = False) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.resolve, pending_id, decision, remember)

    def block_all(self, reason: str) -> None:
        """Fail every waiting request closed (used on shutdown)."""
        self._closing = True
        for fut, _ in list(self._pending.values()):
            if not fut.done():
                fut.set_result(DecisionResult(BLOCK, reason, "shutdown"))


def _audit_fields(call: dict[str, Any]) -> dict[str, Any]:
    return {k: call.get(k) for k in ("source", "server", "method", "tool", "request_id", "arguments")}


# --------------------------------------------------------------------------- HTTP API


def build_app(manager: DecisionManager, *, token: str, port: int,
              on_shutdown_request: Callable[[], None] | None = None) -> web.Application:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @web.middleware
    async def security(request: web.Request, handler):
        # Reject browser-originated requests (CSRF / DNS-rebinding defence): the
        # only legitimate clients are our own proxies, which never send Origin.
        if request.headers.get("Origin") is not None or request.host not in allowed_hosts:
            return web.json_response({"error": "forbidden"}, status=403)
        if request.path != "/v1/health":
            supplied = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pid": os.getpid()})

    async def decisions(request: web.Request) -> web.Response:
        try:
            call = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(call, dict) or not call.get("method"):
            return web.json_response({"error": "invalid request description"}, status=400)
        result = await manager.decide(call)
        return web.json_response(result.to_json())

    async def pending(_: web.Request) -> web.Response:
        return web.json_response({"pending": manager.pending_summary()})

    async def shutdown(_: web.Request) -> web.Response:
        if on_shutdown_request is not None:
            asyncio.get_running_loop().call_later(0.1, on_shutdown_request)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[security], client_max_size=MAX_REQUEST_BYTES)
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/decisions", decisions)
    app.router.add_get("/v1/pending", pending)
    app.router.add_post("/v1/shutdown", shutdown)
    return app


class BrokerServer:
    """Runs the aiohttp app on a private asyncio loop in a worker thread."""

    def __init__(self, manager: DecisionManager, *, host: str, port: int,
                 on_shutdown_request: Callable[[], None]) -> None:
        self.manager = manager
        self.host = host
        self.port = port
        self.token = new_token()
        self.on_shutdown_request = on_shutdown_request
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="mcp-guard-broker", daemon=True)
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._runner: web.AppRunner | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> None:
        self._thread.start()
        self._started.wait(15)
        if self._start_error is not None:
            raise self._start_error
        if not self._started.is_set():
            raise RuntimeError("broker server did not start in time")
        write_broker_info(BrokerInfo(self.host, self.port, self.token, os.getpid()))

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        finally:
            self.loop.close()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        app = build_app(self.manager, token=self.token, port=self.port,
                        on_shutdown_request=self.on_shutdown_request)
        # handler_cancellation: if a proxy disconnects, cancel its pending
        # decision so the stale popup disappears.
        self._runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, self.host, self.port, reuse_address=False).start()
        except BaseException as exc:  # e.g. port in use -> another broker is running
            self._start_error = exc
            self._started.set()
            await self._runner.cleanup()
            return
        self.manager._loop = asyncio.get_running_loop()
        self._started.set()
        await self._stop.wait()
        self.manager.block_all("MCP Guard is shutting down")
        await asyncio.sleep(0.2)  # let blocked responses flush to proxies
        await self._runner.cleanup()

    def stop(self) -> None:
        remove_broker_info(self.token)
        if self._stop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)


def run_broker(*, host: str = DEFAULT_BROKER_HOST, port: int = DEFAULT_BROKER_PORT,
               timeout_s: float = DEFAULT_DECISION_TIMEOUT_S) -> int:
    """Entry point for ``python -m mcp_guard broker``.  Blocks until shutdown."""
    from .ui import TkUI  # imported lazily so headless tests never need a display

    # Single-instance guarantee: binding the fixed port fails if another broker
    # is already running, so no separate PID/lock bookkeeping is needed.
    ui = TkUI()
    manager = DecisionManager(ui, timeout_s=timeout_s)
    ui.on_decision = manager.resolve_threadsafe
    server = BrokerServer(manager, host=host, port=port, on_shutdown_request=ui.quit)
    try:
        server.start()
    except OSError as exc:
        log.error("cannot listen on %s:%s (%s). Is another MCP Guard broker running?", host, port, exc)
        ui.destroy()
        return 1

    def _signal(*_: object) -> None:
        ui.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass

    log.info("MCP Guard broker listening on http://%s:%s (pid %s)", host, port, os.getpid())
    try:
        ui.run()
    finally:
        log.info("broker shutting down")
        server.stop()
        ui.destroy()
    return 0

