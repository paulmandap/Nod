"""Wake-word matching, including the false positives that matter.

Matching is fuzzy because Whisper renders "hey Nod" a dozen ways depending on
the microphone and how fast it was said. Fuzziness is also how you end up
waking on "he nodded and walked away", so both directions are tested here --
the negative cases are the ones that regress silently.

    python datasets\tests\test_wake.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.listen import wake_split                       # noqa: E402

SHOULD_WAKE = [
    ("Hey Nod.", ""),
    ("hey nod", ""),
    ("Hey, Nod!", ""),
    ("Hey not.", ""),
    ("Hey node?", ""),
    ("Hey Naud.", ""),
    ("Hey, nod, attend my meeting.", "attend my meeting"),
    ("hey nod attend my meeting", "attend my meeting"),
    ("Hey not, what's on my calendar?", "whats on my calendar"),
    ("Hey Nod, join my 10:30.", "join my 1030"),
    ("okay so hey Nod attend my meeting", "attend my meeting"),
    ("Hi Nod.", ""),
]

# Every one of these is a real decode from ~/.nod/perf.log, logged while the
# user said "hey Nod" and Nod did not answer. The matcher caught 7 of 18
# attempts that session. They are kept verbatim, spelling and all, because the
# point of this block is that it is not a guess about how Whisper might mishear
# the name -- it is how it did.
SHOULD_WAKE_FIELD = [
    ("Hey, none.", ""),
    ("Hey, none?", ""),
    ("Hey, Nun!", ""),
    ("Hey, Nahn.", ""),
    ("Hey Null.", ""),
    ("Hey, Nud.", ""),
    ("Hey, Nud?", ""),
    ("Hey, Nawd.", ""),
    ("Hey, not play me a music.", "play me a music"),
    ("Hei na, go to Facebook using Brave Browser",
     "go to facebook using brave browser"),
    # Barge-in: said over the top of an answer, so the remainder has to survive.
    ("Hey, none stop.", "stop"),
    ("Hey nerd stop.", "stop"),
]

SHOULD_NOT_WAKE = [
    "the vendor SOC2 audit will not finish this month",
    "and then we deglaze with the wine",
    "Scotty Pippen Jr. eyes leadership role for Memphis Grizzlies",
    "hey there everyone welcome back to the channel",
    "he nodded and walked away",          # the regression this suite exists for
    "I need to head out",
    "",
    "hey guys",
    "so the launch slipped to November",
    # The cost of accepting "none", "nun" and "null" as the name: they are also
    # ordinary English. These are the sentences that pay it.
    "hey none of that matters now",
    "there was none of it left",
    "hey none that was the whole point",
    "the nun was very kind about it",
    "a null pointer exception is thrown",
    "hey nerd was what they called him",
    "hi Ned from accounting called",
    "they nodded along politely",
    "hey Nate can you review this",
    "hey nobody told me about that",
    "he needed a break",
    "hey now hold on a second",
]


def main() -> int:
    failures = 0

    print("SHOULD WAKE")
    for text, want in SHOULD_WAKE:
        woke, rem = wake_split(text)
        ok = woke and rem == want
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {text!r:45} -> {woke}, {rem!r}")

    print("\nSHOULD WAKE (recorded field misses)")
    for text, want in SHOULD_WAKE_FIELD:
        woke, rem = wake_split(text)
        ok = woke and rem == want
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {text!r:45} -> {woke}, {rem!r}")

    print("\nSHOULD NOT WAKE")
    for text in SHOULD_NOT_WAKE:
        woke, _ = wake_split(text)
        failures += 1 if woke else 0
        print(f"  {'FAIL' if woke else 'OK  '} {text!r:55} -> {woke}")

    total = len(SHOULD_WAKE) + len(SHOULD_WAKE_FIELD) + len(SHOULD_NOT_WAKE)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
