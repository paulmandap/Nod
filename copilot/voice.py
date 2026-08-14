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

import os
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

# Piper voices live here, one .onnx plus its .json each. Downloaded with:
#     python -m piper.download_voices en_GB-alan-medium
# from inside ~/.nod/voices. Kept beside the Whisper models rather than in the
# project, so the folder a user can delete to start over holds everything.
VOICES_DIR = Path(os.environ.get("NOD_HOME", Path.home() / ".nod")) / "voices"

VOICE_HINT = "David"     # the SAPI male voice; override with --voice Zira
RATE = 0                 # SAPI scale is -10..10. 0 is the engine's normal
                         # pace; 1 sounded brisk in isolation and turned out
                         # to be tiring to listen to over whole sentences.


class Speaker(threading.Thread):
    """Drains bus.speech and says each line out loud, one at a time."""

    daemon = True

    def __init__(self, bus: Bus, voice: str | None = None,
                 rate: int | None = None, engine: str | None = None,
                 voice_model: str | None = None) -> None:
        super().__init__(name="speaker")
        self.bus = bus
        self.voice = voice or VOICE_HINT
        self.rate = RATE if rate is None else rate
        # "piper" for the local neural voice, "sapi" for the Windows one, or
        # None to prefer piper and fall back when its model is not downloaded.
        self.engine = engine
        self.voice_model = voice_model
        self._piper = None
        self._piper_failed = False
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

    # -- the local neural voice ------------------------------------------
    def _piper_model(self) -> Path | None:
        """The .onnx to speak with, or None if none has been downloaded."""
        if self.voice_model:
            path = Path(self.voice_model)
            return path if path.exists() else None
        folder = VOICES_DIR
        if not folder.is_dir():
            return None
        # Newest first, so downloading a second voice switches to it.
        found = sorted(folder.glob("*.onnx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return found[0] if found else None

    def _ensure_piper(self):
        """Load the voice once, and warm it up before anyone is waiting.

        The warm-up is the whole reason this is not lazy. Measured on this
        machine, the first synthesis after loading takes 2.2 seconds while
        onnxruntime builds its execution plan; every one after it takes 100 to
        300 ms, about 14x realtime. Paying that 2.2 s at startup keeps the
        promise the SAPI path was built around -- that "Yes?" lands
        immediately -- and paying it lazily would spend it on exactly the first
        acknowledgement, which is the one that matters most.
        """
        if self._piper is not None or self._piper_failed:
            return self._piper
        if self.engine == "sapi":
            self._piper_failed = True
            return None

        model = self._piper_model()
        if not model:
            if self.engine == "piper":
                self.bus.say("voice: no piper model in ~/.nod/voices")
            self._piper_failed = True
            return None

        try:
            from piper import PiperVoice

            self.bus.say(f"voice: loading {model.stem}")
            voice = PiperVoice.load(str(model))
            list(voice.synthesize("Ready."))          # warm onnxruntime up
            self._piper = voice
            self.bus.say(f"voice: {model.stem}")
        except Exception as exc:
            self.bus.say(f"voice: piper unavailable ({type(exc).__name__})")
            self._piper_failed = True
        return self._piper

    def _speak_piper(self, voice, sentences: list[str]) -> None:
        """Synthesise and play, in chunks small enough to stop mid-word.

        SAPI could only be interrupted between sentences, because Speak()
        blocks until the whole string is done. Here the audio is played in
        ~100 ms slices with the cancel flag checked between them, so "stop"
        takes effect almost immediately rather than at the next full stop.
        """
        import numpy as np
        import soundcard as sc

        rate = voice.config.sample_rate
        speaker = sc.default_speaker()
        slice_frames = max(1, rate // 10)

        with speaker.player(samplerate=rate, blocksize=slice_frames) as player:
            for i, sentence in enumerate(sentences):
                if self._cancel.is_set():
                    break
                self.current_line = sentence
                if i == 0:
                    perf.mark("speech.start", sentence[:40])

                for chunk in voice.synthesize(sentence):
                    if self._cancel.is_set():
                        break
                    samples = np.asarray(chunk.audio_int16_array,
                                         dtype=np.int16).astype(np.float32)
                    samples /= 32768.0
                    for at in range(0, len(samples), slice_frames):
                        if self._cancel.is_set():
                            break
                        player.play(samples[at:at + slice_frames])

    # -- speaking --------------------------------------------------------
    def say_now(self, text: str) -> None:
        """Speak `text`, sentence by sentence, stoppable part-way through."""
        flat = " ".join(text.split())
        if not flat:
            return
        sentences = [s for s in _SENTENCE.split(flat) if s.strip()]

        with self._lock:
            voice = self._ensure_piper()
            proc = None if voice else self._ensure()
            if not voice and not proc:
                return
            self._cancel.clear()
            self.speaking = True
            try:
                if voice:
                    self._speak_piper(voice, sentences)
                else:
                    for i, sentence in enumerate(sentences):
                        if self._cancel.is_set():
                            break
                        self.current_line = sentence
                        if i == 0:
                            perf.mark("speech.start", sentence[:40])
                        proc.stdin.write(sentence + "\n")
                        proc.stdin.flush()
                        proc.stdout.readline()  # blocks until the worker is done
            except Exception as exc:
                if voice:
                    # Fall back rather than going mute: a dead audio device or
                    # a bad model should cost the neural voice, not the voice.
                    self.bus.say(f"voice: piper failed ({type(exc).__name__}), "
                                 f"using Windows speech")
                    self._piper = None
                    self._piper_failed = True
                else:
                    self._proc = None        # pipe died; next call respawns
            finally:
                self.speaking = False
                self.current_line = ""
                self._cancel.clear()

    def run(self) -> None:
        # Load and warm the voice before the first thing anyone asks it to say.
        if not self._ensure_piper():
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
