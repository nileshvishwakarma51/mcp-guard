"""Full end-to-end run of MCP Guard with REAL popups and REAL mouse/keyboard input (Windows).

Nothing inside MCP Guard is mocked: the broker is the real auto-started
background process and every decision is made by physically clicking the Tk
dialogs (the script finds each popup by its window title, locates the green
Allow / red Block button on screen and clicks it with the real mouse).

    pip install -e ".[dev]" pillow
    python scripts/e2e_real_clicks.py                 # 27 checks, about 1 minute
    python scripts/e2e_real_clicks.py --claude-code   # also drive the real Claude Code CLI

Do not touch the mouse while it runs.  Uses an isolated MCP_GUARD_HOME, so a
real installation is not affected.  The filesystem scenario needs Node (npx).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import asynccontextmanager
from ctypes import wintypes
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from PIL import ImageGrab

PROJECT = Path(__file__).resolve().parents[1]
PY = sys.executable
SCRATCH = Path(tempfile.gettempdir()) / "mcp_guard_e2e"
SCRATCH.mkdir(exist_ok=True)
HOME = SCRATCH / "e2e_home"
SANDBOX = SCRATCH / "e2e_fs_sandbox"
ENV = dict(os.environ, MCP_GUARD_HOME=str(HOME), PYTHONPATH=str(PROJECT))
os.environ["MCP_GUARD_HOME"] = str(HOME)

GREEN, RED = (31, 122, 58), (179, 38, 30)
RESULTS: list[tuple[str, bool, str]] = []

# ------------------------------------------------------------------ Win32 helpers
ctypes.windll.shcore.SetProcessDpiAwareness(1)
user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = -1, 2, 1, 0x40
WM_CLOSE, VK_ESCAPE = 0x0010, 0x1B


def guard_windows() -> dict[int, str]:
    found: dict[int, str] = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith("MCP Guard: allow"):
                found[hwnd] = buf.value
        return True
    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def wait_window(fragment: str, timeout: float = 60) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, title in guard_windows().items():
            if fragment in title:
                return hwnd
        time.sleep(0.1)
    raise TimeoutError(f"no popup containing {fragment!r}; open: {list(guard_windows().values())}")


def wait_count(n: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(guard_windows()) >= n:
            return
        time.sleep(0.1)
    raise TimeoutError(f"expected {n} popups, have {len(guard_windows())}")


def wait_gone(hwnd: int, timeout: float = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hwnd not in guard_windows():
            return True
        time.sleep(0.1)
    return False


def rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def raise_window(hwnd: int) -> None:
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.4)


def click_at(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up


def find_colour(hwnd: int, colour: tuple[int, int, int]) -> tuple[int, int]:
    l, t, r, b = rect(hwnd)
    img = ImageGrab.grab().crop((l, t, r, b)).convert("RGB")
    px = img.load()
    xs, ys = [], []
    for y in range(0, img.height, 2):
        for x in range(0, img.width, 2):
            p = px[x, y]
            if all(abs(p[i] - colour[i]) <= 6 for i in range(3)):
                xs.append(x); ys.append(y)
    if len(xs) < 50:
        raise RuntimeError(f"button colour {colour} not visible in popup")
    xs.sort(); ys.sort()
    return l + xs[len(xs) // 2], t + ys[len(ys) // 2]


def click_button(fragment: str, button: str) -> str:
    hwnd = wait_window(fragment)
    title = guard_windows()[hwnd]
    raise_window(hwnd)
    x, y = find_colour(hwnd, GREEN if button == "allow" else RED)
    click_at(x, y)
    if not wait_gone(hwnd):
        raise RuntimeError(f"popup {title!r} did not close after clicking {button}")
    return title


def close_with_x(fragment: str) -> None:
    hwnd = wait_window(fragment)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # what the title-bar X sends
    if not wait_gone(hwnd):
        raise RuntimeError("popup did not close on WM_CLOSE")


def press_escape(fragment: str) -> None:
    hwnd = wait_window(fragment)
    raise_window(hwnd)
    l, t, r, b = rect(hwnd)
    click_at(l + 60, t + 75)  # click the heading text to focus the dialog
    time.sleep(0.3)
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    user32.keybd_event(VK_ESCAPE, 0, 2, 0)
    if not wait_gone(hwnd):
        raise RuntimeError("popup did not close on Esc")


# ------------------------------------------------------------------ MCP helpers
def text_of(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


@asynccontextmanager
async def stdio_session(command: str, args: list[str], env: dict | None = None):
    params = StdioServerParameters(command=command, args=args, env=env or ENV)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), 180)
            await s.list_tools()
            yield s


async def call_and(session, tool, args, action):
    """Start a tool call, perform a real UI action on its popup, return the result."""
    task = asyncio.create_task(session.call_tool(tool, args))
    await asyncio.to_thread(action)
    return await asyncio.wait_for(task, 60)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


async def scenario(name, coro):
    try:
        await coro
    except Exception as exc:
        traceback.print_exc()
        check(name, False, f"exception: {exc!r}")


def guard(*args: str) -> str:
    return subprocess.run([PY, "-m", "mcp_guard", *args], env=ENV, capture_output=True, text=True).stdout.strip()


def broker_pid() -> int | None:
    try:
        return json.loads((HOME / "broker.json").read_text())["pid"]
    except Exception:
        return None


# ------------------------------------------------------------------ scenarios
DEMO = ["-m", "mcp_guard", "stdio", "--name", "guard-demo", "--", PY, "-m", "demo_server"]
NOTES = HOME / "demo_notes"


async def s_desktop_basic():
    async with stdio_session(PY, DEMO) as s:
        r = await call_and(s, "get_weather", {"city": "Mumbai"},
                           lambda: click_button("get_weather on guard-demo", "allow"))
        check("Allow click -> tool runs (get_weather)", not r.isError and "sunny in Mumbai" in text_of(r), text_of(r))

        r = await call_and(s, "write_note", {"title": "e2e", "content": "hello"},
                           lambda: click_button("write_note", "allow"))
        check("Allow click -> side effect happens (note written)", (NOTES / "e2e.txt").exists(), text_of(r))

        r = await call_and(s, "delete_note", {"title": "e2e"}, lambda: click_button("delete_note", "block"))
        check("Block click -> denial returned to Claude", r.isError and "denied by user" in text_of(r), text_of(r))
        check("Block click -> tool never executed (note still exists)", (NOTES / "e2e.txt").exists())

        r = await call_and(s, "send_email", {"to": "a@b.c", "subject": "s", "body": "b"},
                           lambda: close_with_x("send_email"))
        check("Window X button -> blocked", r.isError and "denied by user" in text_of(r), text_of(r))

        r = await call_and(s, "get_weather", {"city": "Goa"}, lambda: press_escape("get_weather"))
        check("Esc key -> blocked", r.isError and "denied by user" in text_of(r), text_of(r))


async def s_parallel():
    async with stdio_session(PY, DEMO) as s:
        calls = {
            "get_weather": ({"city": "Chennai"}, "allow"),
            "write_note": ({"title": "parallel", "content": "x"}, "allow"),
            "delete_note": ({"title": "e2e"}, "block"),
            "send_email": ({"to": "x@y.z", "subject": "s", "body": "b"}, "block"),
        }
        tasks = {t: asyncio.create_task(s.call_tool(t, a)) for t, (a, _) in calls.items()}

        def act():
            wait_count(4)
            order = []
            for tool in reversed(list(calls)):  # decide in reverse order of arrival
                click_button(f"{tool} on", calls[tool][1])
                order.append(tool)
            return order
        order = await asyncio.to_thread(act)
        check("4 simultaneous popups opened", True, f"clicked in order {order}")
        results = {t: await asyncio.wait_for(task, 60) for t, task in tasks.items()}
        for tool, (_, decision) in calls.items():
            r = results[tool]
            ok = (not r.isError) if decision == "allow" else (r.isError and "Blocked" in text_of(r))
            check(f"parallel: {tool} -> {decision}", ok, text_of(r)[:80])
        check("parallel: blocked delete did not run", (NOTES / "e2e.txt").exists())


async def s_desktop_install_flow():
    """Do what Claude Desktop does: read the (installed) config and launch each entry."""
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir()
    (SANDBOX / "hello.txt").write_text("hello from sandbox")
    cfg = SCRATCH / "claude_desktop_config.json"
    original = {"mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", str(SANDBOX)]},
        "guard-demo": {"command": PY, "args": ["-m", "demo_server"]},
    }}
    cfg.write_text(json.dumps(original, indent=2))
    out = subprocess.run([PY, "-m", "mcp_guard", "install", "--config", str(cfg)], env=ENV,
                         capture_output=True, text=True).stdout
    wrapped = json.loads(cfg.read_text())
    entry = wrapped["mcpServers"]["filesystem"]
    check("install wraps config entries", entry["args"][:3] == ["-m", "mcp_guard", "stdio"]
          and wrapped["mcpServers"]["guard-demo"]["args"][:3] == ["-m", "mcp_guard", "stdio"], out.strip().splitlines()[-1])

    async with stdio_session(entry["command"], entry["args"], dict(ENV, **entry.get("env", {}))) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        check("real Node filesystem server starts behind the guard (npx on Windows)",
              {"list_directory", "write_file"} <= names, f"{len(names)} tools")
        r = await call_and(s, "list_directory", {"path": str(SANDBOX)},
                           lambda: click_button("list_directory on filesystem", "allow"))
        check("filesystem: Allow list_directory -> real result", "hello.txt" in text_of(r), text_of(r)[:60])
        r = await call_and(s, "write_file", {"path": str(SANDBOX / "evil.txt"), "content": "pwned"},
                           lambda: click_button("write_file on filesystem", "block"))
        check("filesystem: Block write_file -> file NOT created",
              r.isError and not (SANDBOX / "evil.txt").exists(), text_of(r)[:70])

    subprocess.run([PY, "-m", "mcp_guard", "uninstall", "--config", str(cfg)], env=ENV, capture_output=True)
    check("uninstall restores the exact original config", json.loads(cfg.read_text()) == original)


async def s_http():
    demo = subprocess.Popen([PY, "-m", "demo_server", "--http", "--port", "9411"], env=ENV,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy = subprocess.Popen([PY, "-m", "mcp_guard", "http", "--upstream", "http://127.0.0.1:9411",
                              "--name", "web-demo", "--port", "8911", "--secret-path", "e2esecret"],
                             env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(5)
        NOTES.mkdir(parents=True, exist_ok=True)
        (NOTES / "parallel.txt").write_text("must survive")
        async with streamable_http_client("http://127.0.0.1:8911/e2esecret/mcp") as (r_, w_, _):
            async with ClientSession(r_, w_) as s:
                await asyncio.wait_for(s.initialize(), 30)
                await s.list_tools()
                r = await call_and(s, "get_weather", {"city": "Kolkata"},
                                   lambda: click_button("get_weather on web-demo", "allow"))
                check("HTTP (Claude Web path): Allow click", not r.isError and "Kolkata" in text_of(r), text_of(r))
                r = await call_and(s, "delete_note", {"title": "parallel"},
                                   lambda: click_button("delete_note on web-demo", "block"))
                check("HTTP (Claude Web path): Block click", r.isError and (NOTES / "parallel.txt").exists(),
                      text_of(r))
    finally:
        proxy.terminate(); demo.terminate()


async def s_broker_crash():
    async with stdio_session(PY, DEMO) as s:
        task = asyncio.create_task(s.call_tool("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}))
        hwnd = await asyncio.to_thread(wait_window, "send_email")
        pid = broker_pid()
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        r = await asyncio.wait_for(task, 30)
        check("broker killed mid-request -> request blocked (fail-closed)",
              r.isError and "unavailable" in text_of(r), text_of(r)[:90])
        check("popup disappears with the dead broker", await asyncio.to_thread(wait_gone, hwnd))
        r = await call_and(s, "get_weather", {"city": "Agra"}, lambda: click_button("get_weather", "allow"))
        check("next request auto-restarts the broker and works", not r.isError and broker_pid() != pid,
              f"old pid {pid}, new pid {broker_pid()}")


async def s_timeout():
    guard("stop")
    await asyncio.sleep(1.5)
    subprocess.Popen([PY.replace("python.exe", "pythonw.exe"), "-m", "mcp_guard", "broker", "--timeout", "5"],
                     env=ENV, creationflags=0x00000008 | 0x00000200 | 0x01000000)
    await asyncio.sleep(3)
    async with stdio_session(PY, DEMO) as s:
        task = asyncio.create_task(s.call_tool("delete_note", {"title": "parallel"}))
        hwnd = await asyncio.to_thread(wait_window, "delete_note")
        t0 = time.time()
        r = await asyncio.wait_for(task, 30)
        check("no click -> auto-blocked after timeout", r.isError and "no decision within 5s" in text_of(r),
              f"{text_of(r)} (after {time.time() - t0:.1f}s)")
        check("timed-out popup closes itself", await asyncio.to_thread(wait_gone, hwnd))


async def s_stop_with_pending():
    async with stdio_session(PY, DEMO) as s:
        task = asyncio.create_task(s.call_tool("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}))
        await asyncio.to_thread(wait_window, "send_email")
        status = guard("status")
        check("status shows the pending request", "1 pending" in status and "send_email" in status,
              status.splitlines()[0])
        guard("stop")
        r = await asyncio.wait_for(task, 30)
        check("stop with pending request -> it is blocked, not left hanging",
              r.isError and "shutting down" in text_of(r), text_of(r))
    await asyncio.sleep(1)
    check("broker fully stopped", "not running" in guard("status"))


async def s_claude_code():
    """Drive the real Claude Code CLI (a real Anthropic MCP client) through MCP Guard."""
    claude = shutil.which("claude")
    if not claude:
        check("Claude Code CLI available", False, "claude not on PATH")
        return
    NOTES.mkdir(parents=True, exist_ok=True)
    (NOTES / "claude-code-test.txt").write_text("must survive")
    cfg = SCRATCH / "claude_code_mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"guard-demo": {
        "command": PY,
        "args": ["-m", "mcp_guard", "stdio", "--name", "guard-demo", "--source", "claude-code", "--",
                 PY, "-m", "demo_server"],
        "env": {"MCP_GUARD_HOME": str(HOME), "PYTHONPATH": str(PROJECT)}}}}))
    prompt = ("Call the guard-demo get_weather tool for the city Pune. Then call the guard-demo delete_note tool "
              "with title 'claude-code-test'. Do not call any other tools. Finally report, for each of the two "
              "calls, the exact text the tool returned.")
    proc = await asyncio.create_subprocess_exec(
        claude, "-p", prompt, "--mcp-config", str(cfg), "--strict-mcp-config",
        "--allowedTools", "mcp__guard-demo__get_weather,mcp__guard-demo__delete_note",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(SCRATCH))
    stop = False
    clicked: list[str] = []

    def clicker():
        while not stop:
            for _hwnd, title in list(guard_windows().items()):
                decision = "allow" if "get_weather" in title else "block"
                try:
                    click_button(title, decision)
                    clicked.append(f"{decision}: {title}")
                except Exception as exc:  # the window may vanish meanwhile
                    print("clicker:", exc)
            time.sleep(0.2)

    worker = asyncio.create_task(asyncio.to_thread(clicker))
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), 300)
    finally:
        stop = True
        await worker
    text = out.decode("utf-8", "replace")
    print("--- Claude Code answered:\n" + text.strip()[:800])
    events = [json.loads(l) for l in (HOME / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    cc = [e for e in events if e.get("event") == "decision" and e.get("source") == "claude-code"]
    check("Claude Code: tool calls went through MCP Guard", len(cc) >= 2, f"popups clicked: {clicked}")
    check("Claude Code: get_weather allowed and executed",
          any(e["tool"] == "get_weather" and e["decision"] == "allow" for e in cc) and "sunny" in text.lower())
    check("Claude Code: delete_note blocked, note still on disk",
          any(e["tool"] == "delete_note" and e["decision"] == "block" for e in cc)
          and (NOTES / "claude-code-test.txt").exists())
    check("Claude Code: model was told the call was blocked", "block" in text.lower())


async def main():
    if HOME.exists():
        shutil.rmtree(HOME)
    guard("stop")
    saved = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(saved))
    t0 = time.time()
    for name, coro in [("desktop basic", s_desktop_basic()), ("parallel", s_parallel()),
                       ("install flow + filesystem", s_desktop_install_flow()), ("http", s_http()),
                       ("broker crash", s_broker_crash()), ("timeout", s_timeout()),
                       ("stop", s_stop_with_pending())] + (
                           [("claude code", s_claude_code())] if "--claude-code" in sys.argv else []):
        print(f"\n=== {name}", flush=True)
        await scenario(name, coro)
    user32.SetCursorPos(saved.x, saved.y)
    guard("stop")
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n==== {passed}/{len(RESULTS)} checks passed in {time.time() - t0:.0f}s ====")
    audit = (HOME / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    decisions = [json.loads(l) for l in audit if '"event": "decision"' in l]
    print("audit decisions by:", {k: sum(d["decided_by"] == k for d in decisions)
                                  for k in {d["decided_by"] for d in decisions}})
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
