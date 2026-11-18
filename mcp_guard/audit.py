"""Append-only JSON-lines audit trail of every decision the broker makes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import audit_log_file
from .protocol import MAX_ARGUMENT_PREVIEW_CHARS


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or audit_log_file()
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        if len(line) > MAX_ARGUMENT_PREVIEW_CHARS * 2:
            entry["arguments"] = "<omitted: too large>"
            line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            # Auditing must never take down request handling.
            pass
