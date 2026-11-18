"""Logging that is safe for stdio MCP: never writes to stdout."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import log_dir


def setup_logging(component: str, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(f"%(asctime)s {component}[%(process)d] %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    file_handler = logging.handlers.RotatingFileHandler(log_dir() / f"{component}.log", maxBytes=2_000_000,
                                                        backupCount=3, encoding="utf-8", delay=True)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # stderr is safe for every component; for stdio proxies Claude Desktop
    # captures it into its per-server MCP log.  pythonw has no stderr.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
