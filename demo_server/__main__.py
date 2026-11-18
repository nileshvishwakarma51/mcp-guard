"""A small, harmless MCP server used to test and demo MCP Guard.

    python -m demo_server                       # stdio   (Claude Desktop)
    python -m demo_server --http --port 9000    # Streamable HTTP at /mcp (Claude Web via tunnel)

Notes are stored as text files in ~/.mcp_guard/demo_notes so a demo can show
that a *blocked* ``delete_note`` really left the file in place.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

NOTES_DIR = Path(os.environ.get("MCP_GUARD_HOME") or Path.home() / ".mcp_guard") / "demo_notes"


def _note_path(title: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip()[:80] or "untitled"
    return NOTES_DIR / f"{safe}.txt"


def build_server(host: str = "127.0.0.1", port: int = 9000) -> FastMCP:
    mcp = FastMCP("guard-demo", host=host, port=port,
                  instructions="Demo tools for testing MCP Guard approvals.")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_weather(city: str) -> str:
        """Get the (fake) current weather for a city."""
        return f"It is 24°C and sunny in {city}."

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_notes() -> list[str]:
        """List the titles of all saved notes."""
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        return sorted(p.stem for p in NOTES_DIR.glob("*.txt"))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def write_note(title: str, content: str) -> str:
        """Save a note to disk (overwrites a note with the same title)."""
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        path = _note_path(title)
        path.write_text(content, encoding="utf-8")
        return f"Saved note '{path.stem}' ({len(content)} characters) to {path}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def delete_note(title: str) -> str:
        """Permanently delete a saved note."""
        path = _note_path(title)
        if not path.exists():
            return f"No note named '{path.stem}'."
        path.unlink()
        return f"Deleted note '{path.stem}'."

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=True, destructiveHint=False))
    def send_email(to: str, subject: str, body: str) -> str:
        """Send an email (simulated: nothing is actually sent)."""
        return f"[simulated] Email to {to} with subject '{subject}' sent."

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def slow_echo(text: str, seconds: float = 1.0) -> str:
        """Echo text back after a delay (useful for concurrency tests)."""
        await asyncio.sleep(max(0.0, min(seconds, 30.0)))
        return text

    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(prog="demo_server")
    ap.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    a = ap.parse_args()
    server = build_server(a.host, a.port)
    server.run("streamable-http" if a.http else "stdio")


if __name__ == "__main__":
    main()
