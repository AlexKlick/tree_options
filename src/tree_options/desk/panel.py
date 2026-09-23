"""Reading the research panel under its writers' lock.

``artifacts/paper-trades/fetch_ohlc.py`` (run by eod-equity, by hand, or by
the research chain) holds an EXCLUSIVE flock on ``ohlc-panel.json.lock``
around each read/merge/write transaction (added 2026-09-23 after the Codex
review: two writers used to overwrite each other's merged sessions). The
desk takes the same lock SHARED to read, so it never sees a panel between
a writer's re-read and its replace, and never holds it while fetch_ohlc.py
runs (that would deadlock the child).
"""

from __future__ import annotations

import fcntl
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOCK_SUFFIX = ".lock"  # fetch_ohlc.py: PANEL.with_name(PANEL.name + ".lock")
POLL_S = 0.1


class PanelLocked(RuntimeError):
    """A writer held the panel lock past the timeout."""


def lock_path(panel: Path) -> Path:
    return panel.with_name(panel.name + LOCK_SUFFIX)


def read_panel(
    panel: Path,
    *,
    timeout_s: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The parsed panel, read under a shared lock (OSError/ValueError pass
    through for a missing or torn file; PanelLocked on timeout)."""
    if not panel.exists():
        raise FileNotFoundError(str(panel))
    deadline = monotonic() + timeout_s
    with open(lock_path(panel), "a") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    raise PanelLocked(f"{panel.name}: a writer holds the lock") from None
                sleep(POLL_S)
        doc = json.loads(panel.read_text())
    if not isinstance(doc, dict):
        raise ValueError(f"{panel.name}: not a panel object")
    return doc
