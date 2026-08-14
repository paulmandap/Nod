"""Filler detection — the "let me finish my sentence" guard.

Hearing only "uhmmm" means the instruction is still coming, so the listening
window is held open rather than sending a thinking noise off to be interpreted
as a command. The failure this prevents is Nod answering before you have said
what you want.

    python datasets\tests\test_filler.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.listen import CommandListener                  # noqa: E402


class Listener(CommandListener):
    def __init__(self):
        pass


ONLY_FILLER = ["uhmmm", "um", "so", "uh um hmm", "okay", "well", "hmm",
               "Uh...", "um, uh", "OK"]

NOT_FILLER = ["fry an egg", "um fry an egg", "attend my meeting",
              "play me some music", "so what is the exchange rate",
              "okay play some jazz"]


def main() -> int:
    listener = Listener()
    failures = 0

    print("ONLY FILLER (window must stay open)")
    for text in ONLY_FILLER:
        got = listener._only_filler(text)
        failures += 0 if got else 1
        print(f"  {'OK  ' if got else 'FAIL'} {text!r:24} -> {got}")

    print("\nREAL COMMANDS (must dispatch)")
    for text in NOT_FILLER:
        got = listener._only_filler(text)
        failures += 1 if got else 0
        print(f"  {'FAIL' if got else 'OK  '} {text!r:34} -> {got}")

    # Empty input is neither: nothing was heard, so there is nothing to hold
    # the window open for.
    if listener._only_filler(""):
        print("  FAIL empty string treated as filler")
        failures += 1

    total = len(ONLY_FILLER) + len(NOT_FILLER) + 1
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
