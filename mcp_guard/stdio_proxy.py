"""stdio interception for Claude Desktop.

Claude Desktop starts every local MCP server as a child process and talks
newline-delimited JSON-RPC over its stdin/stdout.  ``mcp_guard install``
rewrites each server entry so Claude starts *this proxy* instead, and the
proxy starts the real server:

    Claude Desktop ──stdin──▶ StdioProxy ──stdin──▶ real MCP server
                   ◀─stdout──            ◀─stdout──

Client→server messages are inspected.  Intercepted requests (``tools/call``
by default) are parked in their own asyncio task while the user decides, so
other traffic - including other tool calls - keeps flowing.  Everything else,
and every server→client message, is forwarded byte-for-byte.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import shutil
import sys
import threading
from typing import Any

from .gate import Gate
from .protocol import (denial_response, describe, error_response, is_notification, is_request, is_response,
                       should_intercept, tool_catalog_from_list_result)

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024  # max size of a single JSON-RPC line
CHILD_EXIT_GRACE_S = 5.0
OUTBOX_HIGH_WATER = 256  # queued messages before we stop reading from the server


def _id_key(request_id: Any) -> str:
    # 1 and "1" are different JSON-RPC ids; json.dumps keeps them apart.
    return json.dumps(request_id, sort_keys=True)


class StdioProxy:
    def __init__(self, name: str, command: list[str], gate: Gate, *, intercepted: frozenset[str],
                 source: str = "claude-desktop", stdin=None, stdout=None) -> None:
        if not command:
            raise ValueError("no MCP server command given")
        self.name = name
        self.command = command
        self.gate = gate
        self.intercepted = intercepted
        self.source = source
        self._stdin = stdin or sys.stdin.buffer
        self._stdout = stdout or sys.stdout.buffer
        self._held: dict[str, asyncio.Task] = {}  # request id -> task waiting for the user
        self._tools_list_ids: set[str] = set()
        self._tool_catalog: dict[str, dict] = {}
        self._child: asyncio.subprocess.Process | None = None
        self._child_write_lock = asyncio.Lock()
        # Writes to Claude happen on a dedicated thread: a blocking stdout write
        # (Claude reading slowly, a multi-MB result) must never stall the event
        # loop, where other requests are waiting for decisions.
        self._outbox: queue.Queue[bytes | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write_stdout, name="stdout-writer", daemon=True)
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        argv = list(self.command)
        resolved = shutil.which(argv[0])  # finds npx.cmd / uvx.exe etc. on Windows
        if resolved:
            argv[0] = resolved
        log.info("[%s] starting MCP server: %s", self.name, argv)
        try:
            self._child = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None,  # inherit: server logs end up in Claude's MCP log
                limit=STREAM_LIMIT)
        except OSError as exc:
            log.error("[%s] cannot start MCP server %s: %s", self.name, argv, exc)
            return 127

        self._writer.start()
        threading.Thread(target=self._read_stdin, args=(loop,), name="stdin-reader", daemon=True).start()
        client_task = asyncio.create_task(self._pump_client(), name="client->server")
        server_task = asyncio.create_task(self._pump_server(), name="server->client")
        done, _ = await asyncio.wait({client_task, server_task}, return_when=asyncio.FIRST_COMPLETED)

        if client_task in done:
            log.info("[%s] client closed the connection; shutting down server", self.name)
            await self._cancel_held(notify=False)
            await self._stop_child()
            await self._settle(server_task)
        else:
            await self._child.wait()
            log.warning("[%s] MCP server exited (code %s)", self.name, self._child.returncode)
            await self._cancel_held(notify=True)
            client_task.cancel()
            await self._settle(client_task)
        for t in (client_task, server_task):
            if t.done() and not t.cancelled() and t.exception():
                log.error("[%s] %s failed: %r", self.name, t.get_name(), t.exception())
        await self.gate.close()
        await asyncio.to_thread(self._flush_output)
        return self._child.returncode or 0

    def _read_stdin(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking reader thread (asyncio cannot read console/pipe stdin portably on Windows)."""
        try:
            for line in iter(self._stdin.readline, b""):
                loop.call_soon_threadsafe(self._inbox.put_nowait, line)
        except (OSError, ValueError) as exc:
            log.warning("[%s] stdin read failed: %s", self.name, exc)
        finally:
            try:
                loop.call_soon_threadsafe(self._inbox.put_nowait, None)
            except RuntimeError:
                pass  # loop already closed

    async def _stop_child(self) -> None:
        child = self._child
        if child is None or child.returncode is not None:
            return
        try:
            if child.stdin is not None:
                child.stdin.close()  # polite: EOF lets the server exit cleanly
            await asyncio.wait_for(child.wait(), CHILD_EXIT_GRACE_S)
        except (asyncio.TimeoutError, OSError):
            log.warning("[%s] server did not exit after stdin closed; terminating", self.name)
            try:
                child.terminate()
                await asyncio.wait_for(child.wait(), CHILD_EXIT_GRACE_S)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                try:
                    child.kill()
                except (ProcessLookupError, OSError):
                    pass
                await child.wait()

    @staticmethod
    async def _settle(task: asyncio.Task) -> None:
        try:
            await asyncio.wait_for(task, CHILD_EXIT_GRACE_S)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    async def _cancel_held(self, *, notify: bool) -> None:
        tasks = list(self._held.items())
        for key, task in tasks:
            task.cancel()
            if notify:
                self._send_to_client(error_response(json.loads(key), -32000,
                                                    f"MCP server '{self.name}' exited before the request ran"))
        for _, task in tasks:
            await self._settle(task)

    # ------------------------------------------------------------------ I/O
    def _send_to_client(self, payload: dict | list | bytes) -> None:
        data = payload if isinstance(payload, bytes) else (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        self._outbox.put(data)

    def _write_stdout(self) -> None:
        broken = False
        while True:
            data = self._outbox.get()
            if data is None:
                return
            if broken:
                continue  # client is gone; drain silently
            try:
                self._stdout.write(data)
                self._stdout.flush()
            except (OSError, ValueError) as exc:
                log.warning("[%s] cannot write to client: %s", self.name, exc)
                broken = True

    def _flush_output(self) -> None:
        """Deliver everything still queued (e.g. final error replies) before the process exits."""
        self._outbox.put(None)
        self._writer.join(timeout=CHILD_EXIT_GRACE_S)

    async def _send_to_server(self, data: bytes) -> None:
        child = self._child
        if child is None or child.stdin is None or child.returncode is not None:
            raise ConnectionError("MCP server is not running")
        if not data.endswith(b"\n"):
            data += b"\n"
        async with self._child_write_lock:
            child.stdin.write(data)
            await child.stdin.drain()

    # ------------------------------------------------------------------ client -> server
    async def _pump_client(self) -> None:
        while True:
            line = await self._inbox.get()
            if line is None:
                return
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                # Not ours to judge: pass through and let the server reply with a parse error.
                await self._forward(line, None)
                continue
            if isinstance(msg, list):
                await self._handle_batch(msg)
            else:
                await self._handle_message(msg, line)

    async def _forward(self, raw: bytes, msg: Any) -> None:
        """Send to the server; if it is gone, answer requests with an error instead of dying.

        The server->client pump notices the exit separately and runs the normal
        shutdown path, so this only has to keep Claude from waiting forever.
        """
        try:
            await self._send_to_server(raw)
        except (ConnectionError, OSError) as exc:
            log.warning("[%s] cannot forward to server: %s", self.name, exc)
            for item in msg if isinstance(msg, list) else [msg]:
                if is_request(item):
                    self._send_to_client(error_response(item.get("id"), -32000, f"MCP server unavailable: {exc}"))

    async def _handle_message(self, msg: Any, raw: bytes) -> None:
        if is_notification(msg) and msg.get("method") == "notifications/cancelled":
            request_id = (msg.get("params") or {}).get("requestId")
            task = self._held.get(_id_key(request_id))
            if task is not None:
                # The server never saw this request; just withdraw the popup.
                log.info("[%s] client cancelled held request %r", self.name, request_id)
                task.cancel()
                return
        if is_request(msg) and msg.get("method") == "tools/list":
            self._tools_list_ids.add(_id_key(msg.get("id")))
        if should_intercept(msg, self.intercepted):
            self._hold(msg, raw)
            return
        await self._forward(raw, msg)

    async def _handle_batch(self, batch: list) -> None:
        passthrough = []
        for item in batch:
            if should_intercept(item, self.intercepted):
                self._hold(item, (json.dumps(item, ensure_ascii=False) + "\n").encode())
            else:
                if is_request(item) and item.get("method") == "tools/list":
                    self._tools_list_ids.add(_id_key(item.get("id")))
                passthrough.append(item)
        if passthrough:
            await self._forward((json.dumps(passthrough, ensure_ascii=False) + "\n").encode(), passthrough)

    def _hold(self, msg: dict, raw: bytes) -> None:
        key = _id_key(msg.get("id"))
        if key in self._held:
            self._send_to_client(error_response(msg.get("id"), -32600, "duplicate request id is already pending"))
            return
        self._held[key] = asyncio.create_task(self._gate_request(key, msg, raw), name=f"gate-{key}")

    async def _gate_request(self, key: str, msg: dict, raw: bytes) -> None:
        try:
            call = describe(msg, source=self.source, server=self.name, tool_catalog=self._tool_catalog)
            allowed, reason = await self.gate.check(call)
        finally:
            self._held.pop(key, None)
        if allowed:
            await self._forward(raw, msg)
        else:
            self._send_to_client(denial_response(msg, reason))

    # ------------------------------------------------------------------ server -> client
    async def _pump_server(self) -> None:
        assert self._child is not None and self._child.stdout is not None
        reader = self._child.stdout
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # a single line exceeded STREAM_LIMIT
                log.error("[%s] oversized message from server dropped", self.name)
                continue
            if not line:
                return
            if self._tools_list_ids:
                self._sniff_tool_catalog(line)
            self._send_to_client(line)
            while self._outbox.qsize() > OUTBOX_HIGH_WATER:  # back-pressure on a slow client
                await asyncio.sleep(0.01)

    def _sniff_tool_catalog(self, line: bytes) -> None:
        """Remember tool descriptions/annotations so the popup can show them."""
        try:
            msg = json.loads(line)
        except ValueError:
            return
        for item in msg if isinstance(msg, list) else [msg]:
            if is_response(item) and _id_key(item.get("id")) in self._tools_list_ids:
                self._tools_list_ids.discard(_id_key(item.get("id")))
                self._tool_catalog.update(tool_catalog_from_list_result(item.get("result")))
