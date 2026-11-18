"""Smoke test of the real Tk popup (skipped automatically on headless machines)."""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")


def _ui():
    from mcp_guard.ui import TkUI
    try:
        return TkUI()
    except tk.TclError as exc:  # no display
        pytest.skip(f"no display: {exc}")


def _pump(ui, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        ui.root.update()
        time.sleep(0.01)


def _buttons(widget):
    for child in widget.winfo_children():
        if isinstance(child, tk.Button):
            yield child
        yield from _buttons(child)


CALL = {"source": "claude-desktop", "server": "demo", "method": "tools/call", "tool": "delete_note",
        "arguments": {"title": "x"}, "timeout_s": 60, "received_at": time.time(),
        "tool_info": {"description": "Delete a note", "annotations": {"destructiveHint": True}}}


def test_popup_is_topmost_and_buttons_report_decisions():
    ui = _ui()
    decisions = []
    ui.on_decision = lambda pid, d, remember: decisions.append((pid, d, remember))
    try:
        ui.show("p1", CALL)
        ui.show("p2", dict(CALL, tool="get_weather"))
        _pump(ui)
        assert set(ui._dialogs) == {"p1", "p2"}
        win = ui._dialogs["p1"].win
        assert win.attributes("-topmost") == 1
        assert win.winfo_viewable()

        allow = next(b for b in _buttons(win) if b.cget("text") == "Allow")
        allow.invoke()
        _pump(ui, 0.2)
        assert decisions == [("p1", "allow", False)]

        # Closing the window with the title-bar X counts as Block.
        win2 = ui._dialogs["p2"].win
        win2.tk.call(win2.protocol("WM_DELETE_WINDOW"))  # exactly what the X button runs
        _pump(ui, 0.2)
        assert decisions[-1] == ("p2", "block", False)
        assert ui._dialogs == {}

        ui.show("p3", CALL)
        _pump(ui, 0.2)
        ui.close("p3")  # broker-side close (timeout / cancellation)
        _pump(ui, 0.2)
        assert ui._dialogs == {} and len(decisions) == 2
    finally:
        ui.destroy()
