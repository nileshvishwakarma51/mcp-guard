"""Wrap / unwrap the MCP servers in ``claude_desktop_config.json``.

Before::

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/x"]}

After::

    "filesystem": {"command": "C:/.../python.exe",
                   "args": ["-m", "mcp_guard", "stdio", "--name", "filesystem", "--",
                            "npx", "-y", "@modelcontextprotocol/server-filesystem", "C:/x"]}

The original command is kept verbatim after ``--``, so ``uninstall`` can
restore it exactly without any side-car state.  A timestamped backup of the
config is written before every change.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

MARKER = ["-m", "mcp_guard", "stdio"]


def candidate_config_paths() -> list[Path]:
    paths: list[Path] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local:  # Microsoft Store (MSIX) installs virtualise %APPDATA%
            paths.extend(sorted(Path(local, "Packages").glob("Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json")))
    elif sys.platform == "darwin":
        paths.append(Path.home() / "Library/Application Support/Claude/claude_desktop_config.json")
    else:
        paths.append(Path.home() / ".config/Claude/claude_desktop_config.json")
    return paths


def find_config(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    candidates = candidate_config_paths()
    for p in candidates:
        if p.exists():
            return p
    if not candidates:
        raise FileNotFoundError("cannot determine the Claude Desktop config location")
    return candidates[0]


def is_wrapped(entry: dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return args[:3] == MARKER


def wrap_entry(name: str, entry: dict[str, Any], python: str, extra: list[str]) -> dict[str, Any]:
    if is_wrapped(entry) or "command" not in entry:
        return entry  # already wrapped, or a remote (url-based) entry we cannot wrap
    new = dict(entry)
    new["command"] = python
    new["args"] = [*MARKER, "--name", name, *extra, "--", entry["command"], *(entry.get("args") or [])]
    return new


def unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not is_wrapped(entry):
        return entry
    args = entry["args"]
    try:
        sep = args.index("--")
    except ValueError:
        return entry
    original = args[sep + 1:]
    if not original:
        return entry
    new = dict(entry)
    new["command"] = original[0]
    new["args"] = original[1:]
    if not new["args"]:
        new.pop("args")
    return new


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig").strip()
    return json.loads(text) if text else {}


def _save(path: Path, data: dict[str, Any]) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def install(config: Path, *, python: str | None = None, only: list[str] | None = None,
            extra: list[str] | None = None, dry_run: bool = False) -> tuple[list[str], dict[str, Any]]:
    data = _load(config)
    servers: dict[str, Any] = data.setdefault("mcpServers", {})
    python = python or sys.executable
    changed = []
    for name, entry in servers.items():
        if only and name not in only:
            continue
        if isinstance(entry, dict) and not is_wrapped(entry) and "command" in entry:
            servers[name] = wrap_entry(name, entry, python, extra or [])
            changed.append(name)
    if changed and not dry_run:
        _save(config, data)
    return changed, data


def uninstall(config: Path, *, dry_run: bool = False) -> tuple[list[str], dict[str, Any]]:
    data = _load(config)
    servers: dict[str, Any] = data.get("mcpServers", {})
    changed = []
    for name, entry in servers.items():
        if isinstance(entry, dict) and is_wrapped(entry):
            servers[name] = unwrap_entry(entry)
            changed.append(name)
    if changed and not dry_run:
        _save(config, data)
    return changed, data


def add_demo_server(config: Path, *, python: str | None = None) -> None:
    """Register the bundled demo server (handy for a first test / the demo video)."""
    data = _load(config)
    servers = data.setdefault("mcpServers", {})
    servers["guard-demo"] = {"command": python or sys.executable, "args": ["-m", "demo_server"]}
    _save(config, data)
