"""Timing markers, off by default.

Nod's latency is spread across five threads and two processes, so "it feels
slow" is not a diagnosis -- the wait between saying "hey Nod" and hearing
"Mhm?" could be endpointing, Whisper, the classifier, or SAPI, and they are not
close to equal. Tagged timestamps make the guess unnecessary.

Off unless NOD_PERF=1, and cheap when off: one env lookup at import, then a
boolean test per call. Nothing here should ever be worth disabling in a hot
loop.

    $env:NOD_PERF = "1"
    python -m copilot.main --agent --local-intent
    python datasets\perf_report.py
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

ENABLED = os.environ.get("NOD_PERF", "") == "1"

_PATH = Path(os.environ.get("NOD_HOME", Path.home() / ".nod")) / "perf.log"
_lock = threading.Lock()


def mark(tag: str, detail: str = "") -> None:
    """Record that `tag` happened now. Never raises -- a broken perf log must
    not take the assistant down with it."""
    if not ENABLED:
        return
    try:
        line = f"{time.monotonic():.4f}\t{tag}\t{detail}\n"
        with _lock:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            with _PATH.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def reset() -> None:
    if not ENABLED:
        return
    try:
        _PATH.unlink(missing_ok=True)
    except Exception:
        pass
