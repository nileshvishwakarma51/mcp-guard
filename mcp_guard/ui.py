"""Always-on-top confirmation dialogs, built on Tkinter (ships with Python).

One ``Toplevel`` window is opened per pending request, so several simultaneous
requests are each visible and can be decided independently and in any order.
Windows are cascaded so none hides another completely.

Safety defaults in the dialog:
* closing the window or pressing Esc counts as **Block**;
* there is deliberately no Enter shortcut, so a user who is typing in another
  app when the dialog pops up cannot approve a request by accident;
* a countdown shows when the request will be auto-blocked.
"""

from __future__ import annotations

import logging
import os
import queue
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable

from .protocol import preview_arguments

log = logging.getLogger(__name__)

POLL_MS = 50
CASCADE_PX = 32
SOURCE_LABELS = {"claude-desktop": "Claude Desktop (stdio)", "claude-code": "Claude Code CLI (stdio)", "claude-web": "Claude Web (remote connector)"}

DecisionCallback = Callable[[str, str, bool], None]


def _enable_hidpi() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


class TkUI:
    """Thread-safe façade: ``show``/``close``/``quit`` may be called from any thread."""

    def __init__(self, on_decision: DecisionCallback | None = None) -> None:
        _enable_hidpi()
        self.on_decision: DecisionCallback = on_decision or (lambda *_: None)
        self.root = tk.Tk()
        self.root.title("MCP Guard")
        self.root.withdraw()  # background app: no main window, only dialogs
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._dialogs: dict[str, _Dialog] = {}
        self._open_order = 0
        self._destroyed = False
        self.root.after(POLL_MS, self._drain)

    # ------------------------------------------------------------ thread-safe API
    def show(self, pending_id: str, call: dict[str, Any]) -> None:
        self._queue.put(("show", pending_id, call))

    def close(self, pending_id: str) -> None:
        self._queue.put(("close", pending_id))

    def quit(self) -> None:
        self._queue.put(("quit",))

    # ------------------------------------------------------------ Tk-thread only
    def run(self) -> None:
        self.root.mainloop()

    def destroy(self) -> None:
        if not self._destroyed:
            self._destroyed = True
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "show":
                    self._open(item[1], item[2])
                elif kind == "close":
                    dlg = self._dialogs.pop(item[1], None)
                    if dlg is not None:
                        dlg.destroy()
                    self._retitle()
                elif kind == "quit":
                    for dlg in list(self._dialogs.values()):
                        dlg.destroy()
                    self._dialogs.clear()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        except Exception:  # never let one bad message kill the UI loop
            log.exception("UI error")
        self.root.after(POLL_MS, self._drain)

    def _open(self, pending_id: str, call: dict[str, Any]) -> None:
        if pending_id in self._dialogs:
            return
        offset = (self._open_order % 8) * CASCADE_PX
        self._open_order += 1
        self._dialogs[pending_id] = _Dialog(self, pending_id, call, offset)
        self._retitle()

    def _decide(self, pending_id: str, decision: str, remember: bool) -> None:
        dlg = self._dialogs.pop(pending_id, None)
        if dlg is not None:
            dlg.destroy()
        self._retitle()
        self.on_decision(pending_id, decision, remember)

    def _retitle(self) -> None:
        total = len(self._dialogs)
        for i, dlg in enumerate(self._dialogs.values(), start=1):
            dlg.set_position(i, total)


class _Dialog:
    def __init__(self, ui: TkUI, pending_id: str, call: dict[str, Any], offset: int) -> None:
        self.ui = ui
        self.pending_id = pending_id
        self.call = call
        self.deadline = float(call.get("received_at") or time.time()) + float(call.get("timeout_s") or 120)
        self._alive = True

        win = self.win = tk.Toplevel(ui.root)
        win.title(f"MCP Guard: allow {call.get('tool') or call.get('method')} on {call.get('server')}?")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: self._click("block"))
        win.bind("<Escape>", lambda _e: self._click("block"))
        win.minsize(520, 380)

        base = tkfont.nametofont("TkDefaultFont")
        bold = base.copy()
        bold.configure(weight="bold")
        title_font = base.copy()
        title_font.configure(size=base.cget("size") + 4, weight="bold")
        self._fonts = (bold, title_font)

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)

        method = call.get("method")
        headline = "Claude wants to run a tool" if method == "tools/call" else f"Claude wants to call {method}"
        ttk.Label(outer, text=headline, font=title_font).pack(anchor="w")
        self.position_label = ttk.Label(outer, text="", foreground="#666")
        self.position_label.pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(outer)
        grid.pack(fill="x")
        rows = [
            ("From", SOURCE_LABELS.get(call.get("source"), str(call.get("source")))),
            ("MCP server", call.get("server")),
            ("Tool", call.get("tool") or "-"),
            ("Method", method),
        ]
        if call.get("client"):
            rows.append(("Client", call.get("client")))
        for r, (label, value) in enumerate(rows):
            ttk.Label(grid, text=f"{label}:", font=bold).grid(row=r, column=0, sticky="nw", padx=(0, 10))
            ttk.Label(grid, text=str(value), wraplength=440).grid(row=r, column=1, sticky="w")

        info = call.get("tool_info") or {}
        description = (info.get("description") or "").strip()
        if description:
            if len(description) > 400:
                description = description[:400] + "..."
            ttk.Label(outer, text=description, wraplength=480, foreground="#444").pack(anchor="w", pady=(8, 0))
        hint = _risk_hint(info.get("annotations"))
        if hint:
            text, colour = hint
            tk.Label(outer, text=text, fg=colour, font=bold).pack(anchor="w", pady=(6, 0))

        ttk.Label(outer, text="Arguments:", font=bold).pack(anchor="w", pady=(10, 2))
        box = tk.Text(outer, height=10, width=70, wrap="word", font=("Consolas", 10), relief="solid", bd=1)
        box.insert("1.0", preview_arguments(call.get("arguments")))
        box.configure(state="disabled")  # display only; never interpreted
        box.pack(fill="both", expand=True)

        self.remember = tk.BooleanVar(value=False)
        tool_name = call.get("tool") or method
        ttk.Checkbutton(outer, variable=self.remember,
                        text=f"Remember my choice for '{tool_name}' on '{call.get('server')}' until MCP Guard restarts"
                        ).pack(anchor="w", pady=(8, 0))

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))
        self.countdown = ttk.Label(bottom, text="", foreground="#a15c00")
        self.countdown.pack(side="left")
        tk.Button(bottom, text="Allow", width=12, bg="#1f7a3a", fg="white", activebackground="#17602d",
                  activeforeground="white", command=lambda: self._click("allow")).pack(side="right")
        tk.Button(bottom, text="Block", width=12, bg="#b3261e", fg="white", activebackground="#8c1d17",
                  activeforeground="white", command=lambda: self._click("block")).pack(side="right", padx=(0, 8))

        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), 560), win.winfo_reqheight()
        x = (win.winfo_screenwidth() - w) // 2 + offset
        y = max((win.winfo_screenheight() - h) // 3 + offset, 0)
        win.geometry(f"{w}x{h}+{x}+{y}")
        self._bring_to_front()
        self._tick()

    def _bring_to_front(self) -> None:
        win = self.win
        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        win.bell()

    def _tick(self) -> None:
        if not self._alive:
            return
        remaining = max(0, int(self.deadline - time.time()))
        self.countdown.configure(text=f"Auto-blocks in {remaining}s")
        # Re-assert topmost only if another program actually removed it.  Setting
        # it unconditionally would also re-order our own dialogs every second,
        # making stacked popups jump over the one the user is about to click.
        if _lost_topmost(self.win):
            self.win.attributes("-topmost", True)
        self.win.after(1000, self._tick)

    def set_position(self, index: int, total: int) -> None:
        if self._alive:
            self.position_label.configure(text=f"Pending request {index} of {total}" if total > 1 else "")

    def _click(self, decision: str) -> None:
        if self._alive:
            self.ui._decide(self.pending_id, decision, bool(self.remember.get()))

    def destroy(self) -> None:
        if self._alive:
            self._alive = False
            try:
                self.win.destroy()
            except tk.TclError:
                pass


def _lost_topmost(win: tk.Toplevel) -> bool:
    """True if the OS-level always-on-top flag is gone (Windows only; Tk caches its own value)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        hwnd = int(win.wm_frame(), 16)
        GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x00000008
        return not (ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception:
        return False


def _risk_hint(annotations: Any) -> tuple[str, str] | None:
    if not isinstance(annotations, dict):
        return None
    if annotations.get("destructiveHint") is True:
        return "Warning: the server marks this tool as DESTRUCTIVE", "#b3261e"
    if annotations.get("readOnlyHint") is True:
        return "The server marks this tool as read-only", "#1f7a3a"
    if annotations.get("openWorldHint") is True:
        return "This tool interacts with external systems", "#a15c00"
    return None


__all__ = ["TkUI"]
