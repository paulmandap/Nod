"""The command window after "Yes?" — measured from when you start talking.

The bug this protects against had no error message and no log line. Nod would
hear "hey Nod", answer "Yes?", and then silently ignore the instruction that
followed. Nothing was broken in the sense of throwing; the command simply
arrived after the awake window had closed and was dropped on the floor.

The window was checked at *dispatch* time, so everything between the
acknowledgement and a fully segmented utterance was billed against it: Nod
speaking "Yes?", the user reacting, the sentence itself, and the 1.6 s of
silence HANG_AWAKE waits before believing the sentence ended. A short command
left about a second of headroom. A normal one did not fit.

Timing is simulated rather than slept -- one block is 100 ms by construction,
so block index *is* the clock and the suite still runs instantly.

    python datasets\tests\test_awake_window.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.audio import BLOCK_FRAMES                        # noqa: E402
from copilot.listen import COMMAND_TIMEOUT, CommandListener   # noqa: E402

LOUD = 0.05
QUIET = 0.001
BLOCK_SECONDS = 0.1


def blocks(spec):
    out = []
    for kind, n in spec:
        amp = LOUD if kind == "speech" else QUIET
        for _ in range(n):
            out.append(np.full(BLOCK_FRAMES, amp, dtype=np.float32))
    return out


class Listener(CommandListener):
    """Constants and helpers only -- no thread, no model, no mic."""
    def __init__(self):
        pass


# The window as it shipped when the bug was reported. Kept as a literal so the
# regression case below still demonstrates the failure after COMMAND_TIMEOUT is
# tuned again -- otherwise raising the timeout silently turns that check green
# without the underlying rule being right.
HISTORICAL_TIMEOUT = 8.0


def simulate(spec, window=None):
    """Replay a timeline that begins the moment the ack is queued.

    Returns (dispatched_now, dispatched_at_speech_start), the verdicts the old
    and new rules give for the same audio.
    """
    listener = Listener()
    seg = listener._segmenter()
    awake_until = COMMAND_TIMEOUT if window is None else window
    speech_started = 0.0

    for i, block in enumerate(blocks(spec)):
        now = i * BLOCK_SECONDS
        listener._tune_hang(seg, now < awake_until)

        was_active = seg._active
        utterance = seg.push(block)
        if not was_active and seg._active:
            speech_started = now
        if utterance is None:
            continue

        return (now < awake_until), (speech_started < awake_until)

    return None, None


def check(name, ok, note=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def main() -> int:
    bad = 0
    print(f"COMMAND_TIMEOUT={COMMAND_TIMEOUT}s "
          f"HANG_AWAKE={CommandListener.HANG_AWAKE} blocks\n")

    # 1.0 s of Nod saying "Yes?", 1.2 s of the user drawing breath, then a
    # 4.5 s instruction -- "play me some music from YouTube Music" -- and the
    # silence that ends it. This is an ordinary request, not a pathological one.
    normal = [("quiet", 10), ("quiet", 12), ("speech", 45), ("quiet", 20)]

    print("A normal spoken command after the acknowledgement")
    _, started_in_time = simulate(normal)
    bad += check("dispatches (window judged from speech start)",
                 started_in_time is True)

    # Same audio against the 8 s window that shipped, which is where this was
    # reported: the user starts talking 2.2 s in and is ignored anyway.
    late, started_in_time = simulate(normal, window=HISTORICAL_TIMEOUT)
    bad += check("old rule drops it at the historical 8 s window",
                 late is False,
                 "the regression: closed after the window, mid-sentence")
    bad += check("new rule keeps it even at 8 s", started_in_time is True)

    # A short command still works under both rules -- this is the case that
    # made the bug look intermittent rather than systematic.
    print("\nA short command works either way")
    late, started_in_time = simulate(
        [("quiet", 10), ("quiet", 10), ("speech", 15), ("quiet", 20)])
    bad += check("dispatches", started_in_time is True)
    bad += check("old rule also happened to allow it", late is True)

    # Someone who says nothing for the whole window and then speaks must NOT
    # be dispatched -- that is the false-trigger case the window exists for.
    print("\nSpeech that begins after the window has closed")
    late, started_in_time = simulate(
        [("quiet", int(COMMAND_TIMEOUT / BLOCK_SECONDS) + 15),
         ("speech", 15), ("quiet", 20)])
    bad += check("is not dispatched", started_in_time is False)

    # A long one-breath instruction that begins just inside the window is the
    # whole point: it may take many seconds to say and must still land.
    print("\nA long instruction that starts just inside the window")
    late, started_in_time = simulate(
        [("quiet", int(COMMAND_TIMEOUT / BLOCK_SECONDS) - 8),
         ("speech", 60), ("quiet", 20)])
    bad += check("dispatches", started_in_time is True)

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
