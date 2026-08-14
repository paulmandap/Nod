"""Nod's voice, on the SAPI engine Windows already ships.

`pyttsx3` or `pywin32` would be the obvious way to reach SAPI, but both are
installs, and the reason for picking SAPI over a neural voice was that this
machine already has everything needed. So we keep one PowerShell process warm
running `speaker.ps1`, and hand it one line of text per utterance on stdin.

Two details in there matter more than they look:

  Warmth. Spawning a PowerShell per utterance costs 300-600 ms of startup, and
  the whole point of a local voice is that "mhm?" lands immediately -- half a
  second of dead air before a two-phoneme acknowledgement reads as a hang
  rather than a reply. Keeping the process alive makes that a one-off at boot.

  Data, not code. The text crosses as a line on stdin and is only ever bound to
  a variable on the other side. Nothing Nod says is parsed as PowerShell, which
  matters because what it says is assembled from calendar entries and speech
  recognition -- a meeting titled `$(rm -r ...)` is read aloud, not run.

`speaking` is exposed because the mic listener needs it: without it Nod hears
its own voice through the microphone and cheerfully transcribes itself.
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
from pathlib import Path

from . import paths, perf
from .bus import Bus

# Long answers are handed over one sentence at a time rather than in one go.
# That is what makes barge-in possible: SAPI's Speak blocks until the whole
# string is finished, so a five-sentence answer is five seconds during which
# nothing can stop it. Sentence by sentence, "hey Nod" takes effect at the next
# gap -- which is roughly where a person would stop talking anyway if you
# interrupted them.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

# Via paths.resource, not Path(__file__): in a PyInstaller build the script is
# unpacked under sys._MEIPASS and __file__ points somewhere it is not.
SCRIPT = paths.resource("copilot", "speaker.ps1")

VOICE_HINT = "David"     # the male voice; override with --voice Zira
RATE = 0                 # SAPI scale is -10..10. 0 is the engine's normal
                         # pace; 1 sounded brisk in isolation and turned out
                         # to be tiring to listen to over whole sentences.


class Speaker(threading.Thread):
    """Drains bus.speech and says each line out loud, one at a time."""

    daemon = True

    def __init__(self, bus: Bus, voice: str | None = None,
                 rate: int | None = None) -> None:
        super().__init__(name="speaker")
        self.bus = bus
        self.voice = voice or VOICE_HINT
        self.rate = RATE if rate is None else rate
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        # True from the moment a line is handed over until the worker reports
        # it finished. Read by the mic listener, so it is a plain bool.
        self.speaking = False
        # The sentence currently being spoken, so the mic listener can tell
        # Nod's own voice from the user's. Plain string assignment is atomic.
        self.current_line = ""
        self._cancel = threading.Event()

    def interrupt(self) -> None:
        """Stop after the current sentence and drop anything still queued.

        Called from the mic listener when it hears the wake word mid-answer.
        Draining the queue matters as much as the flag: without it Nod stops
        the sentence it is on and then carries on with the rest of the reply,
        which reads as ignoring you.
        """
        self._cancel.set()
        while True:
            try:
                self.bus.speech.get_nowait()
            except queue.Empty:
                break

    # -- the warm PowerShell --------------------------------------------
    def _spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NoLogo", "-ExecutionPolicy", "Bypass",
             "-File", str(SCRIPT), "-VoiceHint", self.voice, "-Rate", str(self.rate)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if (proc.stdout.readline() or "").strip() != "READY":
            raise RuntimeError("speaker.ps1 did not come up")
        return proc

    def _ensure(self) -> subprocess.Popen | None:
        if self._proc is None or self._proc.poll() is not None:
            try:
                self._proc = self._spawn()
            except Exception as exc:
                self.bus.say(f"voice: unavailable ({exc})")
                self._proc = None
        return self._proc

    # -- speaking --------------------------------------------------------
    def say_now(self, text: str) -> None:
        """Speak `text`, sentence by sentence, stoppable between sentences."""
        flat = " ".join(text.split())
        if not flat:
            return
        sentences = [s for s in _SENTENCE.split(flat) if s.strip()]

        with self._lock:
            proc = self._ensure()
            if not proc:
                return
            self._cancel.clear()
            self.speaking = True
            try:
                for i, sentence in enumerate(sentences):
                    if self._cancel.is_set():
                        break
                    self.current_line = sentence
                    if i == 0:
                        perf.mark("speech.start", sentence[:40])
                    proc.stdin.write(sentence + "\n")
                    proc.stdin.flush()
                    proc.stdout.readline()   # blocks until the worker says DONE
            except Exception:
                self._proc = None            # pipe died; next call respawns
            finally:
                self.speaking = False
                self.current_line = ""
                self._cancel.clear()

    def run(self) -> None:
        self._ensure()
        while not self.bus.stop.is_set():
            try:
                line = self.bus.speech.get(timeout=0.2)
            except queue.Empty:
                continue
            self.say_now(line)

        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("__NOD_EXIT__\n")
                proc.stdin.flush()
                proc.wait(timeout=2)
            except Exception:
                proc.terminate()
