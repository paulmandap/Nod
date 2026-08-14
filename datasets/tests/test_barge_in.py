"""Stopping Nod, and not confusing the browser for the request.

Both of these come straight out of ~/.nod/perf.log.

The stop cases: three times in one session the user said "Stop.", "Hey nerd
stop." and "Hey, none stop." over the top of an answer, and Nod finished the
answer anyway. Two of those failed at the wake matcher (test_wake.py covers
that half); the bare "Stop." failed here, because while Nod is speaking
anything that is not the wake word was dropped as probable echo.

The browser cases: "play a music in brave" was classified play_music with
query "brave", so Nod said "Playing brave." and searched YouTube Music for the
word. The browser is how a request is carried out, never the thing asked for.

    python datasets\tests\test_barge_in.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.agent import names_browser, strip_browser        # noqa: E402
from copilot.bus import Bus                                   # noqa: E402
from copilot.listen import CommandListener                    # noqa: E402
from copilot.voice import Speaker                             # noqa: E402

# Whole utterances that mean "be quiet", accepted mid-answer without the wake
# word because saying six words over an answer to stop it is worse than not
# stopping it.
IS_STOP = [
    "Stop.",
    "stop",
    "Stop, stop.",
    "Be quiet.",
    "Quiet!",
    "Shut up.",
    "Enough.",
    "okay stop",           # a filler in front is still a stop
    "um, stop",
    "Cancel.",
    "Nevermind.",
]

# Must NOT be treated as a bare stop: these are instructions that happen to
# contain a stop word, and swallowing them would lose the request.
NOT_STOP = [
    "stop listening to my meeting",     # a real mode change, goes to the agent
    "stop the music and play jazz",
    "what time does the meeting stop",
    "um, uh",                           # filler only, no stop word in it
    "",
    "play me some music",
    "stop by the store on your way",
]

# (query from the classifier, what should be searched / opened)
STRIP = [
    ("brave", ""),
    ("in brave", ""),
    ("using brave browser", ""),
    ("my brave browser", ""),
    ("chrome", ""),
    ("jazz", "jazz"),
    ("Bohemian Rhapsody Queen", "Bohemian Rhapsody Queen"),
    ("ariana grande in brave", "ariana grande"),
    ("facebook using brave browser", "facebook"),
    ("youtube", "youtube"),
]

NAMES_BROWSER = [
    ("my brave browser", True),
    ("chrome", True),
    ("open edge", True),
    ("facebook", False),
    ("jazz", False),
    ("", False),
]


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def main() -> int:
    bad = 0
    bus = Bus()
    listener = CommandListener(bus, Speaker(bus), "base", "cpu", "int8", None)

    print("Bare stop is accepted mid-answer")
    for text in IS_STOP:
        bad += check(f"{text!r}", listener._is_stop(text) is True)

    print("\nEverything else is not a bare stop")
    for text in NOT_STOP:
        bad += check(f"{text!r}", listener._is_stop(text) is False)

    print("\nBrowser stripped from the request")
    for query, want in STRIP:
        got = strip_browser(query)
        bad += check(f"{query!r} -> {want!r}", got == want,
                     "" if got == want else f"got {got!r}")

    print("\nBrowser named at all")
    for query, want in NAMES_BROWSER:
        bad += check(f"{query!r} -> {want}", names_browser(query) is want)

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
