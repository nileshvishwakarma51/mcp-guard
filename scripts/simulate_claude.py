"""Pretend to be Claude Desktop: launch a guarded MCP server and make tool calls.

Useful to try MCP Guard (and record a demo) without Claude installed:

    python scripts/simulate_claude.py              # one call per demo tool, one at a time
    python scripts/simulate_claude.py --parallel   # fire them all at once -> several popups

The proxy auto-starts the broker, so popups appear for every tools/call.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CALLS = [
    ("get_weather", {"city": "Bengaluru"}),
    ("write_note", {"title": "shopping", "content": "milk, eggs"}),
    ("delete_note", {"title": "shopping"}),
    ("send_email", {"to": "boss@example.com", "subject": "Resignation", "body": "I quit."}),
]


async def main(parallel: bool) -> None:
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "mcp_guard", "stdio", "--name", "guard-demo", "--",
                                         sys.executable, "-m", "demo_server"],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            async def one(name, args):
                print(f"-> calling {name}({args}) ... waiting for your decision")
                r = await s.call_tool(name, args)
                text = " ".join(getattr(c, "text", "") for c in r.content)
                print(f"<- {name}: {'BLOCKED' if r.isError else 'OK'}  {text}")

            if parallel:
                await asyncio.gather(*(one(n, a) for n, a in CALLS))
            else:
                for n, a in CALLS:
                    await one(n, a)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--parallel", action="store_true")
    asyncio.run(main(ap.parse_args().parallel))
