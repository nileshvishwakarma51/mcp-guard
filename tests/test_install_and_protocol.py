from __future__ import annotations

import json

from mcp_guard import protocol
from mcp_guard.install import install, uninstall

ORIGINAL = {
    "globalShortcut": "Ctrl+Space",
    "mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/x"],
                       "env": {"A": "1"}},
        "noargs": {"command": "my-server"},
        "remote": {"url": "https://example.com/mcp"},
    },
}


def test_install_uninstall_roundtrip(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps(ORIGINAL), encoding="utf-8")

    changed, _ = install(cfg, python="PY")
    assert sorted(changed) == ["filesystem", "noargs"]
    data = json.loads(cfg.read_text(encoding="utf-8"))
    fs = data["mcpServers"]["filesystem"]
    assert fs["command"] == "PY"
    assert fs["args"] == ["-m", "mcp_guard", "stdio", "--name", "filesystem", "--",
                          "npx", "-y", "@modelcontextprotocol/server-filesystem", "C:/x"]
    assert fs["env"] == {"A": "1"}
    assert data["mcpServers"]["remote"] == ORIGINAL["mcpServers"]["remote"]
    assert data["globalShortcut"] == "Ctrl+Space"
    assert list(tmp_path.glob("*.bak-*")), "a backup must be written"

    again, _ = install(cfg, python="PY")
    assert again == [], "install must be idempotent"

    restored, _ = uninstall(cfg)
    assert sorted(restored) == ["filesystem", "noargs"]
    assert json.loads(cfg.read_text(encoding="utf-8")) == ORIGINAL


def test_install_creates_missing_config(tmp_path):
    cfg = tmp_path / "sub" / "claude_desktop_config.json"
    changed, data = install(cfg, python="PY")
    assert changed == [] and data == {"mcpServers": {}}


def test_denial_shapes():
    call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "x"}}
    d = protocol.denial_response(call, "denied by user")
    assert d["id"] == 5 and d["result"]["isError"] is True
    assert "denied by user" in d["result"]["content"][0]["text"]
    other = protocol.denial_response({"jsonrpc": "2.0", "id": "a", "method": "resources/read"}, "no")
    assert other["error"]["code"] == protocol.ERROR_BLOCKED


def test_classification():
    methods = frozenset({"tools/call"})
    assert protocol.should_intercept({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, methods)
    assert not protocol.should_intercept({"jsonrpc": "2.0", "method": "tools/call"}, methods)  # notification
    assert not protocol.should_intercept({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, methods)
    assert not protocol.should_intercept([1, 2], methods)


def test_preview_truncates_huge_arguments():
    text = protocol.preview_arguments({"blob": "x" * 50_000}, limit=1000)
    assert len(text) < 1100 and "truncated" in text
