"""Screen capture -> change detection -> OCR.

Two things make this cheap enough to run continuously:

  1. `mss` instead of PIL.ImageGrab or cv2 — it's a thin wrapper over the
     platform's native capture API and grabs a 1080p frame in a few ms.
  2. Only OCR when the pixels actually changed. Tesseract on a full screen
     costs 200-600 ms, so running it every frame would peg a core for nothing.
     A downscaled frame diff gates it.

Output is deduplicated too: slides sit still for minutes, and re-sending the
same text to the LLM wastes tokens and produces identical suggestions.
"""

from __future__ import annotations

import difflib
import re
import threading
import time

import cv2
import mss
import numpy as np
import pytesseract

from . import perf
from .bus import Bus, ScreenText

_WS = re.compile(r"[ \t]+")


class ScreenReader(threading.Thread):
    daemon = True

    PIXEL_DIFF_THRESHOLD = 4.0     # mean abs diff on the 160x90 thumbnail
    TEXT_SIMILARITY_MAX = 0.90     # above this, treat as "same slide"
    MAX_CHARS = 1800               # cap what we hand the LLM

    def __init__(
        self,
        bus: Bus,
        interval: float = 1.0,
        monitor: int = 1,
        region: dict | None = None,
        tesseract_cmd: str | None = None,
        active_window: bool = False,
    ) -> None:
        super().__init__(name="screen-reader")
        self.bus = bus
        self.interval = interval
        self.monitor = monitor
        self.region = region
        self.active_window = active_window
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

        self._thumb: np.ndarray | None = None
        self._last_text = ""
        # Say "no window focused" once, not once per second.
        self._warned_no_window = False
        # Read by supervise.Supervisor: a thread that chose to stop is not a
        # thread that crashed, and must not be restarted in a loop.
        self.stopped_deliberately = False

    # -- preprocessing ---------------------------------------------------
    @staticmethod
    def _prep(bgr: np.ndarray) -> np.ndarray:
        """Tesseract wants dark text on a light background, ~300 DPI.

        The 1.5x upscale looks like an easy thing to skip on a 1080p capture --
        the frame is already large, and dropping it made OCR 1.6x faster. It
        was measured and reverted: on the same frame, skipping it retained only
        13% of the tokens and found 2,556 characters against 3,548.

        The reason is that the upscale is not about the size of the *image*, it
        is about the size of the *text*. UI and slide text sits at 12-14 px
        regardless of screen resolution, which is under what Tesseract reads
        reliably; scaling to ~20 px is what makes it legible. Skip it only for
        a capture that is genuinely already high-DPI, which a desktop is not.
        """
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
        gray = cv2.medianBlur(gray, 3)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        # Dark-mode slides come out inverted; flip if most pixels are black.
        if binary.mean() < 127:
            binary = cv2.bitwise_not(binary)
        return binary

    @staticmethod
    def _clean(raw: str) -> str:
        lines = []
        for line in raw.splitlines():
            line = _WS.sub(" ", line).strip()
            # Drop OCR confetti: single glyphs, punctuation runs.
            if len(line) < 3 or not any(c.isalnum() for c in line):
                continue
            lines.append(line)
        return "\n".join(lines)

    def _mask_hud(self, bgr: np.ndarray, bbox: dict) -> np.ndarray:
        """Blank the overlay out of the frame before it is diffed or read.

        Without this the HUD is inside its own screenshot, so its last
        suggestion becomes input to the next one — the summary stops tracking
        the meeting and starts agreeing with itself. It also defeats the change
        detector, since the rail animates every frame and looks like new
        content.
        """
        rect = self.bus.hud_rect
        if not rect:
            return bgr
        x, y, w, h = rect
        # Screen coords -> frame coords; the grab may not start at (0, 0).
        x -= bbox.get("left", 0)
        y -= bbox.get("top", 0)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(bgr.shape[1], x + w), min(bgr.shape[0], y + h)
        if x1 > x0 and y1 > y0:
            bgr[y0:y1, x0:x1] = 0
        return bgr

    def _changed(self, bgr: np.ndarray) -> bool:
        thumb = cv2.cvtColor(
            cv2.resize(bgr, (160, 90), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2GRAY,
        ).astype(np.float32)
        if self._thumb is None:
            self._thumb = thumb
            return True
        diff = float(np.mean(np.abs(thumb - self._thumb)))
        self._thumb = thumb
        return diff > self.PIXEL_DIFF_THRESHOLD

    # -- loop ------------------------------------------------------------
    def run(self) -> None:
        # Check Tesseract before the loop rather than discovering it is missing
        # on the first changed frame. Previously the TesseractNotFoundError
        # surfaced a second or two after startup, was caught by the blanket
        # handler below, and killed this thread for the whole session with one
        # status line that the HUD stops drawing as soon as any card appears.
        # OCR then "just did nothing" with no explanation anywhere.
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            self.bus.say("screen: Tesseract not installed — screen reading is off")
            # Tells the supervisor this was a decision, not a crash, so it does
            # not restart the thread every five seconds to fail the same way.
            self.stopped_deliberately = True
            return

        try:
            with mss.mss() as sct:
                full_screen = sct.monitors[self.monitor]
                bbox = self.region or full_screen
                self.bus.say(f"screen: {bbox['width']}x{bbox['height']}")

                while not self.bus.stop.is_set():
                    if not self.bus.meeting_on.is_set():
                        if not self.bus.idle(self.bus.meeting_on):
                            break
                        continue
                    time.sleep(self.interval)

                    # Re-query each cycle when following the focused window --
                    # it moves, and pygetwindow is cheap.
                    #
                    # When nothing usable is focused this SKIPS the cycle. It
                    # used to fall back to the full monitor, silently: someone
                    # who passed --ocr-active-window specifically to keep their
                    # desktop out of a Gemini prompt got their whole desktop
                    # sent to Gemini, with no message, every second. Capturing
                    # nothing is the only correct reading of that flag.
                    if self.active_window:
                        found = active_window_region()
                        if not found:
                            if not self._warned_no_window:
                                self._warned_no_window = True
                                self.bus.say("screen: no window focused — "
                                             "not reading")
                            continue
                        self._warned_no_window = False
                        if found != bbox:
                            self.bus.say(
                                f"screen: {found['width']}x{found['height']}")
                        bbox = found

                    frame = np.asarray(sct.grab(bbox))[:, :, :3]  # BGRA -> BGR
                    frame = self._mask_hud(frame.copy(), bbox)

                    if not self._changed(frame):
                        continue

                    ocr_started = time.time()
                    text = self._clean(
                        pytesseract.image_to_string(
                            self._prep(frame), config="--oem 3 --psm 6"
                        )
                    )
                    perf.mark("ocr.done",
                              f"{(time.time()-ocr_started)*1000:.0f}ms "
                              f"{bbox['width']}x{bbox['height']}")
                    if len(text) < 20:
                        continue

                    ratio = difflib.SequenceMatcher(
                        None, self._last_text, text
                    ).quick_ratio()
                    if ratio > self.TEXT_SIMILARITY_MAX:
                        continue

                    self._last_text = text
                    self.bus.put_drop_oldest(
                        self.bus.screen, ScreenText(text[: self.MAX_CHARS]))
                    self.bus.say(f"ocr: {len(text)} chars")
        except Exception as exc:
            self.bus.say(f"screen: stopped ({exc})")


def active_window_region() -> dict | None:
    """Bounding box of the focused window, or None if nothing usable is focused.

    Two failure modes used to collapse into the same None, and they want
    opposite responses: "pygetwindow is not installed / this platform cannot do
    it" is a broken setup, while "the desktop is focused right now" is normal
    and momentary. The ImportError now propagates so the caller's guard logs it
    once and the supervisor can react; only the ordinary case returns None.

    Worth knowing, and documented rather than papered over: mss grabs the screen
    pixels inside a rectangle, not the window's own surface. Anything overlapping
    the focused window is inside the capture too. --ocr-active-window reduces
    what gets sent; it does not guarantee only that window is sent.
    """
    import pygetwindow as gw          # ImportError is a real problem: let it out

    try:
        w = gw.getActiveWindow()
    except Exception:
        # pygetwindow raises on some shell states (the desktop focused, a full
        # screen app switching). That is the ordinary case, not a broken setup.
        return None

    if w and w.width > 200 and w.height > 200:
        return {"left": w.left, "top": w.top, "width": w.width, "height": w.height}
    return None
