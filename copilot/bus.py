"""Event types and the queues that connect the worker threads.

Every stage of the pipeline is a thread that reads from one queue and writes to
another. Nothing shares mutable state except ContextStore, which has its own
lock. This keeps the Qt main thread free to render.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import List


@dataclass
class AudioBlock:
    """A short slab of mono float32 audio at SAMPLE_RATE."""
    samples: "object"  # np.ndarray, typed loosely to keep numpy out of imports
    ts: float = field(default_factory=time.time)


@dataclass
class TranscriptSegment:
    text: str
    duration: float
    ts: float = field(default_factory=time.time)


@dataclass
class ScreenText:
    """OCR output from the captured region, already deduplicated."""
    text: str
    ts: float = field(default_factory=time.time)


@dataclass
class Suggestion:
    """What the HUD renders. Keep it short — this is read in peripheral vision."""
    lead: str                       # one line, <= ~90 chars
    points: List[str] = field(default_factory=list)   # 0-3 terse lines
    kind: str = "summary"           # summary | reply | term | agent
    # What the microphone heard, verbatim, for cards the agent raises. Speech
    # recognition is the least reliable link in the chain and the only one the
    # user can correct, so when Nod acts on something it shows what it thought
    # it was told. "Koto Facebook" on screen explains an odd answer instantly;
    # without it the answer just looks wrong for no reason. Empty for the
    # summariser, which is reporting on the room rather than on an instruction.
    heard: str = ""
    ts: float = field(default_factory=time.time)


class Bus:
    def __init__(self) -> None:
        # Bounded: if transcription falls behind, drop audio rather than
        # accumulate a growing backlog of stale meeting audio.
        self.audio: "Queue[AudioBlock]" = Queue(maxsize=256)
        # Bounded, like the audio queue and for the same reason. These used to
        # be unbounded, which was fine only for as long as the router thread
        # stayed alive to drain them -- and the router had no exception handler
        # at all, so any error killed it silently and left these growing until
        # the process ran out of memory. The supervisor now restarts it, but a
        # queue that cannot grow without limit is the cheaper guarantee.
        #
        # 512 transcript segments is over an hour of speech; if anything is that
        # far behind, the oldest items are of no use to a two-minute rolling
        # context window anyway.
        self.transcripts: "Queue[TranscriptSegment]" = Queue(maxsize=512)
        self.screen: "Queue[ScreenText]" = Queue(maxsize=256)
        self.suggestions: "Queue[Suggestion]" = Queue(maxsize=64)
        self.status: "Queue[str]" = Queue(maxsize=256)

        # --- agent side ---
        # Lines for Nod to say out loud, and utterances heard on the *mic*
        # (as opposed to `transcripts`, which is what the speakers are playing).
        # Keeping the two apart is the whole trick: the assistant must never
        # take the meeting it is listening to as an instruction.
        self.speech: "Queue[str]" = Queue()
        self.heard: "Queue[str]" = Queue()
        # Raw mic blocks, kept apart from self.audio for the same reason.
        self.mic_audio: "Queue[AudioBlock]" = Queue(maxsize=256)
        # "hide yourself" by voice. An Event rather than a direct call so the
        # agent thread never touches a Qt widget; the HUD's own timer drains it.
        self.hud_hide = threading.Event()

        # --- runtime modes ---
        # Which halves of Nod are currently doing work. Every worker starts at
        # boot and idles behind its flag rather than being created and
        # destroyed, because a Python Thread cannot be restarted once its run()
        # returns -- so "turn the summariser back on" would otherwise mean
        # reloading a Whisper model and a fresh process.
        #
        # meeting_on: speaker capture, transcription, screen OCR, summaries.
        # agent_on:   microphone, wake word, commands.
        self.meeting_on = threading.Event()
        self.agent_on = threading.Event()

        self.stop = threading.Event()
        # Set by the hotkey handler to force an LLM call immediately.
        self.force_refresh = threading.Event()

        # Where the HUD currently is, as a plain (x, y, w, h) tuple or None.
        # The screen reader blanks this region before OCR: the overlay is
        # always-on-top, so a full-screen grab contains it, and without this
        # the HUD reads its own last suggestion back in as if it were context
        # and keeps confirming itself. Deliberately a bare tuple rather than a
        # Qt call — the screen reader must never touch a widget from its thread.
        self.hud_rect: tuple[int, int, int, int] | None = None

    def toggle_meeting(self) -> None:
        """Flip meeting listening. Safe from any thread -- Event only."""
        if self.meeting_on.is_set():
            self.meeting_on.clear()
            self.say("meeting: stopped listening")
        else:
            self.meeting_on.set()
            self.say("meeting: listening")

    def toggle_agent(self) -> None:
        """Flip the microphone half. Safe from any thread -- Event only.

        The counterpart to toggle_meeting, and it was missing: meeting mode had
        a hotkey and a voice command, while agent mode could only be set by
        --agent at startup. Turning it on mid-session works because
        CommandListener idles on this flag rather than exiting -- the first
        enable pays the wake model's load, later ones are instant.

        Deliberately not reachable by voice. "Hey Nod, stop listening to me" is
        a one-way door: the thing that would hear you turn it back on is the
        thing being turned off.
        """
        if self.agent_on.is_set():
            self.agent_on.clear()
            self.say("agent: mic off, not listening for 'hey Nod'")
        else:
            self.agent_on.set()
            self.say("agent: listening for 'hey Nod'")

    def idle(self, flag: threading.Event, seconds: float = 0.25) -> bool:
        """Wait while `flag` is clear. True once it is set, False on shutdown.

        The shape every gated worker loop wants: sleep in small steps so a
        toggle takes effect quickly, but never miss bus.stop.
        """
        while not flag.is_set():
            if self.stop.wait(seconds):
                return False
        return True

    def put_drop_oldest(self, q: Queue, item) -> None:
        """Never block a realtime capture thread."""
        try:
            q.put_nowait(item)
        except Exception:
            try:
                q.get_nowait()
                q.put_nowait(item)
            except Exception:
                pass

    def say(self, msg: str) -> None:
        self.put_drop_oldest(self.status, msg)
