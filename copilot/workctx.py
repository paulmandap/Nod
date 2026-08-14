"""What the user is currently working on.

This exists for one scenario: someone asks you a direct question in a meeting
and you have three seconds to answer. The overlay could already tell you a
question had been asked, but not what to say back -- it knew what was in the
room and nothing about what you had been doing all week.

That gap cannot be closed from the transcript. It needs a source of truth about
your own work, and the cheapest one that stays current is a file you can add to
by voice mid-meeting ("hey Nod, note that the batch layer is blocked on the
vendor audit").

Deliberately a plain markdown file, not a database:

  * You can open and edit it while Nod is running.
  * You can see exactly what is being sent to the API on your behalf, which
    matters -- this content leaves the machine with every summariser call.
  * If it is empty, everything degrades to the old behaviour rather than
    breaking.

Capped at MAX_CHARS because it rides along on a request that already carries
two minutes of transcript and a screenful of OCR, and the summariser fires
every few seconds. The newest lines are kept: what you noted ten minutes ago is
more likely to be what the meeting is about than what you noted last Tuesday.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from pathlib import Path

NOD_HOME = Path(os.environ.get("NOD_HOME", Path.home() / ".nod"))
PATH = NOD_HOME / "workcontext.md"

MAX_CHARS = 1200

TEMPLATE = """# What I'm working on

Nod reads this when someone asks you a question in a meeting, and uses it to
suggest answers you could actually give. Keep it short and current -- a few
lines about live work beats a full project history.

Add to it by voice: "hey Nod, note that ..."

"""

_lock = threading.Lock()
_cache: tuple[float, str] | None = None


def read() -> str:
    """Current work context, or "" if there is none.

    mtime-cached: this is read on the summariser's hot path, which fires every
    few seconds, and the file changes maybe twice an hour.
    """
    global _cache
    try:
        stamp = PATH.stat().st_mtime
    except OSError:
        return ""

    if _cache and _cache[0] == stamp:
        return _cache[1]

    try:
        text = PATH.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Keep the tail: the most recent notes are the relevant ones.
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:]
        text = text[text.find("\n") + 1:]      # don't start mid-line

    _cache = (stamp, text)
    return text


def append(line: str) -> bool:
    """Add one dated note. Returns False if it could not be written."""
    line = " ".join(line.split())
    if not line:
        return False
    stamp = dt.date.today().isoformat()
    try:
        with _lock:
            NOD_HOME.mkdir(parents=True, exist_ok=True)
            if not PATH.exists():
                PATH.write_text(TEMPLATE, encoding="utf-8")
            with PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"- ({stamp}) {line}\n")
        return True
    except OSError:
        return False
