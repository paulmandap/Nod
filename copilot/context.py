"""The one piece of shared state: a rolling window of what's been said and shown.

The LLM stage reads a snapshot of this. Keeping it time-bounded rather than
turn-bounded matters — in a fast meeting, two minutes is roughly the span a
person can still act on, and it keeps the prompt small enough to stay cheap.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .bus import ScreenText, TranscriptSegment


class ContextStore:
    def __init__(self, window_seconds: float = 120.0) -> None:
        self.window = window_seconds
        self._lock = threading.Lock()
        self._segments: deque[TranscriptSegment] = deque()
        self._screen: ScreenText | None = None
        self._chars_since_snapshot = 0

    def add_transcript(self, seg: TranscriptSegment) -> None:
        with self._lock:
            self._segments.append(seg)
            self._chars_since_snapshot += len(seg.text)
            self._evict()

    def add_screen(self, shot: ScreenText) -> None:
        with self._lock:
            self._screen = shot
            self._chars_since_snapshot += 200   # a slide change is a real event

    def _evict(self) -> None:
        cutoff = time.time() - self.window
        while self._segments and self._segments[0].ts < cutoff:
            self._segments.popleft()

    @property
    def pending_chars(self) -> int:
        with self._lock:
            return self._chars_since_snapshot

    def snapshot(self) -> tuple[str, str]:
        """Returns (transcript, screen_text) and resets the pending counter."""
        with self._lock:
            self._evict()
            transcript = " ".join(s.text for s in self._segments).strip()
            screen = self._screen.text if self._screen else ""
            self._chars_since_snapshot = 0
            return transcript, screen
