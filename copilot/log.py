"""The crash log, which in a packaged build is the only diagnostic that exists.

`perf.py` is the model for how this behaves -- never raises, never takes the app
down with it -- but it differs in one decisive way: perf logging is off unless
NOD_PERF=1, and this is **always on**.

That is because of what a frozen, windowed build is like to support. There is no
console. A traceback printed to a stream that does not exist is gone. The user
is not technical and will report "it stopped working". `~/.nod/nod.log` plus the
doctor's report is the entire diagnosis, so it has to be written without anybody
having thought to turn it on first.

Capped at 1 MB, rotated once. A log that grows without limit on someone else's
machine is its own bug.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path

_HOME = Path(os.environ.get("NOD_HOME", Path.home() / ".nod"))
PATH = _HOME / "nod.log"

MAX_BYTES = 1_000_000
_lock = threading.Lock()


def _write(line: str) -> None:
    try:
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            if PATH.exists() and PATH.stat().st_size > MAX_BYTES:
                # One generation back. Two files is enough to catch "it broke,
                # I restarted it, now tell me what happened the first time".
                PATH.replace(PATH.with_suffix(".log.1"))
            with PATH.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def say(message: str) -> None:
    """One timestamped line."""
    _write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")


def exception(where: str, exc: BaseException) -> None:
    """A full traceback, tagged with where it came from."""
    body = "".join(traceback.format_exception(type(exc), exc,
                                              exc.__traceback__))
    _write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}  EXCEPTION in {where}\n"
           f"{body}\n")


def hook(exc_type, exc, tb) -> None:
    """sys.excepthook. Anything unhandled on the main thread lands here."""
    exception("main thread", exc)


def thread_hook(args) -> None:
    """threading.excepthook. Without this a worker dies leaving no trace."""
    name = getattr(args.thread, "name", "?")
    if args.exc_value is not None:
        exception(f"thread {name}", args.exc_value)


def install() -> None:
    import sys

    sys.excepthook = hook
    threading.excepthook = thread_hook
    say(f"--- Nod starting (pid {os.getpid()}) ---")
