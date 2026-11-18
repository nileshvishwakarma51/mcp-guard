"""Command line entry point: ``python -m mcp_guard <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

from . import __version__
from .config import (DEFAULT_BROKER_HOST, DEFAULT_BROKER_PORT, DEFAULT_DECISION_TIMEOUT_S,
                     DEFAULT_INTERCEPTED_METHODS, TOKEN_HEADER, read_broker_info)
from .logging_setup import setup_logging

log = logging.getLogger("mcp_guard")


def _methods(value: str | None) -> frozenset[str]:
    if not value:
        return DEFAULT_INTERCEPTED_METHODS
    return frozenset(m.strip() for m in value.split(",") if m.strip())


def cmd_broker(a: argparse.Namespace) -> int:
    setup_logging("broker", a.verbose)
    from .broker import run_broker
    return run_broker(host=a.host, port=a.port, timeout_s=a.timeout)


def cmd_stdio(a: argparse.Namespace) -> int:
    setup_logging(f"stdio-{a.name}", a.verbose)
    from .broker_client import BrokerClient
    from .gate import Gate
    from .stdio_proxy import StdioProxy

    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        print("mcp_guard stdio: missing server command after --", file=sys.stderr)
        return 2
    gate = Gate(BrokerClient(autostart=not a.no_autostart), fail_open=a.fail_open)
    proxy = StdioProxy(a.name, command, gate, intercepted=_methods(a.intercept), source=a.source)
    try:
        code = asyncio.run(proxy.run())
    except KeyboardInterrupt:
        code = 130
    # The stdin reader thread may still be blocked in readline() (e.g. when the
    # server crashed but Claude still holds our stdin open).  A normal
    # interpreter shutdown would then fight over the stdin lock and can crash
    # on Windows, so flush everything and leave immediately.
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    logging.shutdown()
    os._exit(code)


def cmd_http(a: argparse.Namespace) -> int:
    setup_logging("http", a.verbose)
    from .broker_client import BrokerClient
    from .gate import Gate
    from .http_proxy import HttpProxy, generate_secret_path, serve_http_proxy

    secret = None if a.no_secret else (a.secret_path or generate_secret_path())
    gate = Gate(BrokerClient(autostart=not a.no_autostart), fail_open=a.fail_open)
    proxy = HttpProxy(a.upstream, gate, name=a.name, intercepted=_methods(a.intercept), secret_path=secret)
    prefix = f"/{secret}" if secret else ""
    print(f"MCP Guard HTTP proxy for '{a.name}' -> {a.upstream}")
    print(f"  local URL : http://{a.listen}:{a.port}{prefix}/mcp")
    print(f"  public URL: https://<your-tunnel-host>{prefix}/mcp   (paste this into Claude > Settings > Connectors)")
    if secret:
        print("  The random path segment is the access secret - keep the URL private.")
    sys.stdout.flush()
    try:
        asyncio.run(serve_http_proxy(proxy, a.listen, a.port))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_install(a: argparse.Namespace) -> int:
    from .install import add_demo_server, find_config, install
    config = find_config(a.config)
    if a.with_demo:
        add_demo_server(config)
    changed, data = install(config, only=a.only, dry_run=a.dry_run)
    print(f"Claude Desktop config: {config}")
    if a.dry_run:
        print(json.dumps(data, indent=2))
    print(f"{'Would wrap' if a.dry_run else 'Wrapped'}: {', '.join(changed) or '(nothing to do)'}")
    if changed and not a.dry_run:
        print("Fully quit Claude Desktop (tray icon > Quit) and start it again to apply.")
    return 0


def cmd_uninstall(a: argparse.Namespace) -> int:
    from .install import find_config, uninstall
    config = find_config(a.config)
    changed, _ = uninstall(config, dry_run=a.dry_run)
    print(f"Claude Desktop config: {config}")
    print(f"{'Would restore' if a.dry_run else 'Restored'}: {', '.join(changed) or '(nothing to do)'}")
    return 0


def _broker_call(path: str, method: str = "GET") -> dict | None:
    info = read_broker_info()
    if info is None:
        return None
    req = urllib.request.Request(info.base_url + path, method=method, headers={TOKEN_HEADER: info.token})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - localhost only
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def cmd_status(_: argparse.Namespace) -> int:
    data = _broker_call("/v1/pending")
    if data is None:
        print("MCP Guard broker: not running")
        return 1
    info = read_broker_info()
    print(f"MCP Guard broker: running (pid {info.pid if info else '?'}), {len(data['pending'])} pending request(s)")
    for p in data["pending"]:
        print(f"  - [{p['source']}] {p['server']} / {p['tool'] or p['method']}")
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    if _broker_call("/v1/shutdown", "POST") is None:
        print("MCP Guard broker: not running")
        return 1
    print("MCP Guard broker: stopping")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp_guard", description="Approve or block every MCP request Claude makes.")
    p.add_argument("--version", action="version", version=f"mcp_guard {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common_gate(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--intercept", help="comma-separated JSON-RPC methods to hold (default: tools/call)")
        sp.add_argument("--fail-open", action="store_true",
                        help="ALLOW requests when the broker is unreachable (default: block)")
        sp.add_argument("--no-autostart", action="store_true", help="do not start the broker automatically")
        sp.add_argument("-v", "--verbose", action="store_true")

    b = sub.add_parser("broker", help="run the background approval service + popups")
    b.add_argument("--host", default=DEFAULT_BROKER_HOST)
    b.add_argument("--port", type=int, default=DEFAULT_BROKER_PORT)
    b.add_argument("--timeout", type=float, default=DEFAULT_DECISION_TIMEOUT_S,
                   help="seconds before an unanswered request is blocked")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(func=cmd_broker)

    s = sub.add_parser("stdio", help="wrap a local stdio MCP server (used by Claude Desktop)")
    s.add_argument("--name", required=True, help="server name shown in the popup")
    s.add_argument("--source", default="claude-desktop", choices=["claude-desktop", "claude-code"],
                   help="which Claude app launches this proxy (shown in the popup)")
    common_gate(s)
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args...]")
    s.set_defaults(func=cmd_stdio)

    h = sub.add_parser("http", help="reverse-proxy a Streamable HTTP MCP server (used by Claude Web)")
    h.add_argument("--upstream", required=True, help="origin of the real MCP server, e.g. http://127.0.0.1:9000")
    h.add_argument("--name", default="remote", help="server name shown in the popup")
    h.add_argument("--listen", default="127.0.0.1")
    h.add_argument("--port", type=int, default=8800)
    h.add_argument("--secret-path", help="path secret prefix (default: random)")
    h.add_argument("--no-secret", action="store_true", help="serve without a path secret (local testing only)")
    common_gate(h)
    h.set_defaults(func=cmd_http)

    i = sub.add_parser("install", help="wrap the servers in claude_desktop_config.json")
    i.add_argument("--config", help="path to claude_desktop_config.json (auto-detected)")
    i.add_argument("--only", nargs="*", help="wrap only these server names")
    i.add_argument("--with-demo", action="store_true", help="also register the bundled demo server")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="restore the original claude_desktop_config.json entries")
    u.add_argument("--config")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(func=cmd_uninstall)

    sub.add_parser("status", help="show whether the broker runs and what is pending").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="stop the broker (pending requests are blocked)").set_defaults(func=cmd_stop)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
