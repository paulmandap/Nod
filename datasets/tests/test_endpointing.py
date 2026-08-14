"""Adaptive endpointing, against synthetic audio.

Real speech is not needed to test *when* an utterance is cut -- Segmenter only
ever looks at per-block RMS, so a hand-built envelope of loud and quiet blocks
exercises the exact decision path. That makes this runnable in a second with no
microphone, no model, and no flakiness.

What is being protected: the wake word must close fast (the user waits for
"Mhm?"), while a dictated command must survive a thinking pause. Those pull in
opposite directions, and a single HANG_BLOCKS has to be wrong for one of them.

    python datasets\tests\test_endpointing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.audio import BLOCK_FRAMES                      # noqa: E402
from copilot.listen import CommandListener                  # noqa: E402

LOUD = 0.05      # comfortably over START_RMS (0.020)
QUIET = 0.001    # comfortably under STOP_RMS (0.007)


def blocks(spec: list[tuple[str, int]]) -> list[np.ndarray]:
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


def run(name: str, spec, awake: bool, expect_cut: bool, max_blocks=None):
    listener = Listener()
    seg = listener._segmenter()
    fired_at = None
    for i, block in enumerate(blocks(spec), 1):
        listener._tune_hang(seg, awake)
        if seg.push(block) is not None:
            fired_at = i
            break

    ok = (fired_at is not None) == expect_cut
    if ok and expect_cut and max_blocks is not None:
        ok = fired_at <= max_blocks

    status = "OK  " if ok else "FAIL"
    detail = f"cut at block {fired_at}" if fired_at else "no cut"
    limit = f" (limit {max_blocks})" if max_blocks else ""
    print(f"  {status} {name:52} {detail}{limit}")
    return ok


def main() -> int:
    print(f"HANG_IDLE={CommandListener.HANG_IDLE} "
          f"HANG_AWAKE={CommandListener.HANG_AWAKE} "
          f"LONG_UTTERANCE_BLOCKS={CommandListener.LONG_UTTERANCE_BLOCKS}\n")

    results = [
        # Bare "hey Nod": ~0.8s of speech then silence. Must close on the short
        # hang -- speech(8) + HANG_IDLE(6) = 14 blocks, so anything under ~16
        # proves the idle path is being used and not the 1.6s one.
        run("bare wake word closes fast (idle)",
            [("speech", 8), ("quiet", 12)], awake=False,
            expect_cut=True, max_blocks=16),

        # Same audio while awake: the patient hang applies, so it must NOT have
        # closed by block 14. 8 + 16 = 24.
        run("same audio while awake waits longer",
            [("speech", 8), ("quiet", 6)], awake=True,
            expect_cut=False),

        # A dictated command with a 1.2s thinking pause in the middle must
        # survive the pause and not be cut into two.
        run("awake: 1.2s mid-command pause does not cut",
            [("speech", 10), ("quiet", 12), ("speech", 10)], awake=True,
            expect_cut=False),

        # One-breath "hey Nod, attend my meeting": still idle, but once past
        # LONG_UTTERANCE_BLOCKS the patient hang takes over mid-utterance, so a
        # 1.0s pause inside it must not cut.
        run("idle: long one-breath command survives a 1.0s pause",
            [("speech", 30), ("quiet", 10), ("speech", 5)], awake=False,
            expect_cut=False),

        # Sanity: silence alone never produces an utterance.
        run("silence alone never fires",
            [("quiet", 40)], awake=False, expect_cut=False),
    ]

    failures = results.count(False)
    print(f"\nFAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
