"""Transport-independent MCP / JSON-RPC 2.0 helpers.

Both proxies (stdio for Claude Desktop, HTTP for Claude Web) use these
functions, so interception and denial semantics are identical everywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JSONRPC = "2.0"

#: JSON-RPC server-error range code used when a non-tool request is blocked.
ERROR_BLOCKED = -32001

MAX_ARGUMENT_PREVIEW_CHARS = 20_000


@dataclass
class PendingCall:
    """Everything the user needs to see to decide on one intercepted request."""

    source: str  # "claude-desktop" | "claude-web"
    server: str  # logical MCP server name
    method: str  # e.g. "tools/call"
    request_id: Any  # JSON-RPC id (str | int)
    tool: str | None = None
    arguments: Any = None
    tool_info: dict[str, Any] | None = None  # description / annotations from tools/list
    client: str | None = None  # e.g. user agent, for display only
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "server": self.server,
            "method": self.method,
            "request_id": self.request_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "tool_info": self.tool_info,
            "client": self.client,
            "extra": self.extra,
        }


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def is_response(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" not in msg and "id" in msg


def should_intercept(msg: Any, intercepted: frozenset[str]) -> bool:
    return is_request(msg) and msg.get("method") in intercepted


def describe(msg: dict[str, Any], *, source: str, server: str, tool_catalog: dict[str, dict] | None = None,
             client: str | None = None) -> PendingCall:
    """Extract the human-relevant parts of an intercepted request."""
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {"value": params}
    tool: str | None
    arguments: Any
    if method == "tools/call":
        tool = str(params.get("name", "<unnamed>"))
        arguments = params.get("arguments", {})
    elif method == "resources/read":
        tool, arguments = None, {"uri": params.get("uri")}
    elif method == "prompts/get":
        tool, arguments = str(params.get("name", "")), params.get("arguments", {})
    else:
        tool, arguments = None, params
    info = (tool_catalog or {}).get(tool) if method == "tools/call" and tool else None
    return PendingCall(source=source, server=server, method=method, request_id=msg.get("id"),
                       tool=tool, arguments=arguments, tool_info=info, client=client)


def denial_response(msg: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build the reply sent to Claude instead of forwarding a blocked request.

    For ``tools/call`` we return a *successful* JSON-RPC response carrying a
    ``CallToolResult`` with ``isError: true``.  That is what the MCP spec
    prescribes for tool-level failures, and it lets the model see the reason
    and tell the user, instead of the client surfacing an opaque protocol
    error.  Every other method gets a regular JSON-RPC error object.
    """
    text = f"Blocked by MCP Guard: {reason}"
    if msg.get("method") == "tools/call":
        return {
            "jsonrpc": JSONRPC,
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    return {
        "jsonrpc": JSONRPC,
        "id": msg.get("id"),
        "error": {"code": ERROR_BLOCKED, "message": text},
    }


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": request_id, "error": {"code": code, "message": message}}


def preview_arguments(arguments: Any, limit: int = MAX_ARGUMENT_PREVIEW_CHARS) -> str:
    """Pretty-print arguments for display, truncated so a huge payload cannot hang the UI."""
    try:
        text = json.dumps(arguments, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(arguments)
    if len(text) > limit:
        text = text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"
    return text


def tool_catalog_from_list_result(result: Any) -> dict[str, dict]:
    """Turn a ``tools/list`` result into ``{name: {description, annotations}}``."""
    catalog: dict[str, dict] = {}
    if not isinstance(result, dict):
        return catalog
    for tool in result.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            catalog[tool["name"]] = {
                "description": tool.get("description"),
                "annotations": tool.get("annotations"),
            }
    return catalog
