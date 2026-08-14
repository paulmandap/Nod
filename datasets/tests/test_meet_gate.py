"""The camera gate, against a fake browser tab.

This is the highest-consequence logic in the project: it decides whether to
click Join on a real meeting with real people in it. It is also the logic that
cannot be tested against the real thing without joining a real meeting, so it is
tested against a Tab stand-in that returns canned answers for each JS call.

The three cases that matter, all of which were live bugs:

  1. `data-is-muted` absent and the label in Spanish. deviceState used to test
     only for the English "turn on", so an already-off camera read as ON, and
     _turn_off dutifully clicked it -- turning the camera on a moment before
     joining. Now an unrecognised label is "unknown" and is never clicked.

  2. State unknown. Previously a falsy `isOff` meant "not off", which meant
     click, then join anyway because the camera was never a hard gate. Now
     unknown means do not click and do not join.

  3. A live video track with the label claiming otherwise. cameraLive() reads
     the actual MediaStreamTrack, so no label and no locale can talk it round.

    python datasets\tests\test_meet_gate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot import meet                                     # noqa: E402


class FakeTab:
    """Answers the specific JS expressions meet.py sends.

    Matching on substrings rather than parsing the JS: the point is to exercise
    the Python decision path, not to reimplement a browser.
    """

    def __init__(self, mic="off", camera="off", camera_live=False,
                 in_call=True, has_join=True):
        self.states = {"mic": mic, "camera": camera}
        self.camera_live = camera_live
        self.in_call = in_call
        self.has_join = has_join
        self.clicks: list[str] = []
        self.target_id = "fake"

    def _which(self, expr: str) -> str | None:
        for word in meet.CAM_WORDS:
            if json.dumps(word)[1:-1] in expr:
                return "camera"
        for word in meet.MIC_WORDS:
            if json.dumps(word)[1:-1] in expr:
                return "mic"
        return None

    def js(self, expr: str):
        if "window.__nod = {" in expr:
            return None
        if "cameraLive" in expr:
            return self.camera_live
        if "inCall" in expr:
            return self.in_call
        if "innerText" in expr:
            return False

        kind = self._which(expr)
        if "b.click()" in expr:
            if kind:
                self.clicks.append(kind)
                # Clicking flips it, which is what makes case 1 dangerous:
                # clicking an already-off camera turns it ON.
                self.states[kind] = "on" if self.states[kind] == "off" else "off"
            return True
        if "deviceState" in expr:
            return self.states.get(kind, "unknown")
        if "byLabel" in expr:
            if any(w in expr for w in ("join now", "ask to join")):
                return self.has_join
            return True
        return None

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, tab):
        self._tab = tab
        self.granted = None

    def grant_media(self, origin, camera=False):
        self.granted = (origin, camera)
        return True

    def open(self, url):
        return self._tab


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def run(name, expect_join, expect_camera_clicks=None, **kw):
    tab = FakeTab(**kw)
    browser = FakeBrowser(tab)
    result = meet.join("https://meet.google.com/x", browser=browser,
                       timeout=1.0,
                       require_camera_off=kw.pop("require_camera_off", True))
    bad = check(name, result.ok is expect_join,
                f"ok={result.ok} state={result.state} :: {result.detail[:52]}")
    if expect_camera_clicks is not None:
        got = tab.clicks.count("camera")
        bad += check(f"    ...camera clicked {expect_camera_clicks}x",
                     got == expect_camera_clicks, f"got {got}")
    return bad


def main() -> int:
    bad = 0

    print("Camera state decides whether Nod joins")
    bad += run("both already off -> joins", True, 0,
               mic="off", camera="off")
    bad += run("camera on -> turned off, then joins", True, 1,
               mic="off", camera="on")
    bad += run("camera unknown -> refuses, never clicks", False, 0,
               mic="off", camera="unknown")
    bad += run("camera live despite label -> refuses", False,
               mic="off", camera="off", camera_live=True)
    bad += run("mic unknown -> refuses (mic was always a gate)", False,
               mic="unknown", camera="off")

    print("\nThe opt-out still works")
    tab = FakeTab(mic="off", camera="unknown")
    result = meet.join("https://meet.google.com/x", browser=FakeBrowser(tab),
                       timeout=1.0, require_camera_off=False)
    bad += check("--allow-unconfirmed-camera joins anyway", result.ok is True,
                 f"state={result.state}")
    bad += check("...and never clicked an unknown control",
                 tab.clicks.count("camera") == 0)

    print("\nPost-join verification")
    tab = FakeTab(mic="off", camera="off", in_call=False, has_join=False)
    result = meet.join("https://meet.google.com/x", browser=FakeBrowser(tab),
                       timeout=1.0)
    bad += check("no tiles, no join button -> 'waiting', not 'joined'",
                 result.ok and result.state == "waiting",
                 f"state={result.state}")

    print("\nThe browser is told to deny the camera")
    tab = FakeTab()
    browser = FakeBrowser(tab)
    meet.join("https://meet.google.com/x", browser=browser, timeout=1.0)
    bad += check("grant_media called with camera=False",
                 browser.granted == ("https://meet.google.com", False),
                 str(browser.granted))

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
