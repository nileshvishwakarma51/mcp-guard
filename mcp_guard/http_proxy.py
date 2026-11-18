"""Streamable-HTTP interception for Claude Web (and remote connectors in Desktop).

Claude Web does not run MCP servers on your machine.  A *custom connector*
is a URL that Anthropic's backend calls over MCP's Streamable HTTP
transport.  The browser never sees the MCP traffic, so the only place it can be
intercepted reliably is in front of the MCP server itself.  This module is that
reverse proxy:

    claude.ai ──HTTPS──▶ tunnel (cloudflared/ngrok) ──▶ HttpProxy ──▶ real MCP server
                                                          │
                                                          └─▶ broker popup

Every ``POST`` body is inspected.  Intercepted JSON-RPC requests are held
while the user decides; the HTTP request simply stays open (Streamable HTTP
allows long-running requests).  Allowed requests are forwarded untouched and
the upstream response, including SSE streams, is relayed chunk by chunk.
Blocked requests get the same denial payload the stdio proxy returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from .gate import Gate
from .protocol import (denial_response, describe, error_response, is_notification, is_request, should_intercept,
                       tool_catalog_from_list_result)

log = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "host", "content-length"}
MAX_BODY_BYTES = 8 * 1024 * 1024


class HttpProxy:
    def __init__(self, upstream: str, gate: Gate, *, name: str, intercepted: frozenset[str],
                 secret_path: str | None, source: str = "claude-web") -> None:
        self.upstream = upstream.rstrip("/")
        self.gate = gate
        self.name = name
        self.intercepted = intercepted
        self.secret_path = secret_path.strip("/") if secret_path else None
        self.source = source
        self._session: aiohttp.ClientSession | None = None
        self._tool_catalog: dict[str, dict] = {}
        # (Mcp-Session-Id, json id) -> event that withdraws the pending decision
        self._held: dict[tuple[str, str], asyncio.Event] = {}

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, _: web.Application) -> None:
        # No total timeout: SSE streams and held requests are long-lived by design.
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                                              auto_decompress=True)

    async def _on_cleanup(self, _: web.Application) -> None:
        if self._session is not None:
            await self._session.close()
        await self.gate.close()

    # ------------------------------------------------------------------ routing
    def _upstream_path(self, request: web.Request) -> str | None:
        path = request.rel_url.raw_path
        if self.secret_path is None:
            return path
        prefix = f"/{self.secret_path}"
        if path == prefix or path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
        return None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = self._upstream_path(request)
        if path is None:
            # Unknown path (e.g. internet scanners hitting the tunnel): reveal nothing.
            return web.Response(status=404)
        body = await request.read() if request.can_read_body else b""
        target = self.upstream + path + (f"?{request.rel_url.raw_query_string}" if request.rel_url.raw_query_string else "")

        lists_tools = False
        if request.method == "POST" and body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if parsed is not None:
                self._apply_cancellations(parsed, request)
                denied = await self._gate_body(parsed, request)
                if denied is not None:
                    return web.json_response(denied)
                items = parsed if isinstance(parsed, list) else [parsed]
                lists_tools = any(is_request(m) and m.get("method") == "tools/list" for m in items)
        return await self._forward(request, target, body, sniff_tools=lists_tools)

    def _apply_cancellations(self, parsed: Any, request: web.Request) -> None:
        """Withdraw held requests named by ``notifications/cancelled`` in this body.

        The notification itself is still forwarded upstream; a server that
        never saw the request simply ignores it, as the MCP spec requires.
        """
        session_id = request.headers.get("Mcp-Session-Id", "")
        for m in parsed if isinstance(parsed, list) else [parsed]:
            if is_notification(m) and m.get("method") == "notifications/cancelled":
                key = (session_id, json.dumps((m.get("params") or {}).get("requestId")))
                event = self._held.get(key)
                if event is not None:
                    event.set()

    async def _gate_body(self, parsed: Any, request: web.Request) -> Any | None:
        """Return a denial payload if anything in the body is blocked, else ``None``."""
        items = parsed if isinstance(parsed, list) else [parsed]
        held = [m for m in items if should_intercept(m, self.intercepted)]
        if not held:
            return None
        client = request.headers.get("User-Agent")
        calls = [describe(m, source=self.source, server=self.name, tool_catalog=self._tool_catalog,
                          client=client) for m in held]
        # Over Streamable HTTP a client cancels with a *separate* POST carrying
        # notifications/cancelled, so register this request where that POST
        # can find it and withdraw the pending decision.
        session_id = request.headers.get("Mcp-Session-Id", "")
        cancelled = asyncio.Event()
        keys = [(session_id, json.dumps(m.get("id"))) for m in held]
        for k in keys:
            self._held[k] = cancelled
        checks = asyncio.ensure_future(asyncio.gather(*(self.gate.check(c) for c in calls)))
        waiter = asyncio.ensure_future(cancelled.wait())
        try:
            await asyncio.wait({checks, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for k in keys:
                if self._held.get(k) is cancelled:
                    del self._held[k]
            waiter.cancel()
            if not checks.done():
                checks.cancel()  # closes the popup(s) via the broker connection
        if not checks.done() or checks.cancelled():
            log.info("client cancelled held request(s) %s", [k[1] for k in keys])
            results = [(False, "request was cancelled by the client")] * len(held)
        else:
            results = checks.result()
        if all(allowed for allowed, _ in results):
            return None
        if not isinstance(parsed, list):
            return denial_response(parsed, results[0][1])
        # A JSON-RPC batch is forwarded as one HTTP request, so it cannot be
        # partially executed: if any member is blocked, the whole batch is.
        reasons = {json.dumps(m.get("id")): r for m, (ok, r) in zip(held, results) if not ok}
        out = []
        for m in items:
            if not is_request(m):
                continue
            key = json.dumps(m.get("id"))
            if key in reasons:
                out.append(denial_response(m, reasons[key]))
            else:
                out.append(error_response(m.get("id"), -32001,
                                          "Blocked by MCP Guard: another request in the same batch was blocked"))
        return out

    # ------------------------------------------------------------------ forwarding
    async def _forward(self, request: web.Request, target: str, body: bytes, *, sniff_tools: bool) -> web.StreamResponse:
        assert self._session is not None
        headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP)
        try:
            upstream = await self._session.request(request.method, target, headers=headers, data=body or None,
                                                   allow_redirects=False)
        except aiohttp.ClientError as exc:
            log.error("upstream %s unreachable: %s", target, exc)
            return web.json_response({"error": "upstream MCP server unreachable"}, status=502)

        async with upstream:
            resp_headers = CIMultiDict((k, v) for k, v in upstream.headers.items()
                                       if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding")
            if sniff_tools:
                data = await upstream.read()
                self._learn_tools(data, upstream.headers.get("Content-Type", ""))
                return web.Response(status=upstream.status, headers=resp_headers, body=data)
            response = web.StreamResponse(status=upstream.status, headers=resp_headers)
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
            except (aiohttp.ClientError, ConnectionResetError) as exc:
                log.warning("stream from upstream interrupted: %s", exc)
            await response.write_eof()
            return response

    def _learn_tools(self, data: bytes, content_type: str) -> None:
        texts: list[str] = []
        text = data.decode("utf-8", "replace")
        if "text/event-stream" in content_type:
            texts = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        else:
            texts = [text]
        for t in texts:
            try:
                msg = json.loads(t)
            except ValueError:
                continue
            for item in msg if isinstance(msg, list) else [msg]:
                if isinstance(item, dict) and isinstance(item.get("result"), dict) and "tools" in item["result"]:
                    self._tool_catalog.update(tool_catalog_from_list_result(item["result"]))


def generate_secret_path() -> str:
    return secrets.token_urlsafe(18)


async def serve_http_proxy(proxy: HttpProxy, host: str, port: int) -> None:
    runner = web.AppRunner(proxy.build_app(), handler_cancellation=True, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
