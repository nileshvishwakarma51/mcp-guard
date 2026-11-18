"""Shared configuration: file locations, defaults and broker discovery.

Everything MCP Guard writes lives under one directory (``~/.mcp_guard`` by
default, overridable with the ``MCP_GUARD_HOME`` environment variable, which
the test-suite uses to stay isolated from a real installation).
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 47821

#: How long a request may wait for a human before it is blocked automatically.
DEFAULT_DECISION_TIMEOUT_S = 120.0

#: JSON-RPC methods that are held for user approval by default.  ``tools/call``
#: is the one that actually *does* something; the others can be enabled with
#: ``--intercept`` if an organisation also wants to gate data reads.
DEFAULT_INTERCEPTED_METHODS: frozenset[str] = frozenset({"tools/call"})

TOKEN_HEADER = "X-MCP-Guard-Token"


def guard_home() -> Path:
    home = Path(os.environ.get("MCP_GUARD_HOME") or Path.home() / ".mcp_guard")
    home.mkdir(parents=True, exist_ok=True)
    return home


def discovery_file() -> Path:
    return guard_home() / "broker.json"


def audit_log_file() -> Path:
    return guard_home() / "audit.jsonl"


def log_dir() -> Path:
    d = guard_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class BrokerInfo:
    """What a proxy needs to reach the broker.  Written by the broker on start."""

    host: str
    port: int
    token: str
    pid: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_broker_info(info: BrokerInfo) -> None:
    """Atomically publish broker connection details (readable by this user only)."""
    path = discovery_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(info)), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # best effort; no-op semantics on Windows
    except OSError:
        pass
    os.replace(tmp, path)


def read_broker_info() -> BrokerInfo | None:
    try:
        data = json.loads(discovery_file().read_text(encoding="utf-8"))
        return BrokerInfo(
            host=str(data["host"]),
            port=int(data["port"]),
            token=str(data["token"]),
            pid=int(data["pid"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remove_broker_info(expected_token: str) -> None:
    """Delete the discovery file, but only if it still belongs to us."""
    info = read_broker_info()
    if info is not None and info.token == expected_token:
        try:
            discovery_file().unlink()
        except OSError:
            pass
