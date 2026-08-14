"""Nod's voice: a local neural model, falling back to what Windows ships.

Two engines, chosen automatically.

  Piper is preferred. It runs a small neural voice model on onnxruntime, on the
  CPU, at roughly 14x realtime -- local, free, no API key, no quota, and the
  voices are plain files under ~/.nod/voices that a user can add to or delete.
  This replaced SAPI after a request for a much better voice; a cloud service
  was the obvious alternative and the wrong one, because a network round trip
  per sentence undoes the responsiveness the rest of this file exists to
  protect, and adds a second quota to run out of.

  SAPI stays as the fallback, reached through a warm PowerShell process running
  `speaker.ps1`. It needs no download at all, so it is what speaks on a machine
  where no voice model has been fetched yet.

Three details matter more than they look:

  Warmth. Piper's first synthesis after loading takes 2.2 s while onnxruntime
  builds its execution plan, against 100-300 ms for every one after it; the
  SAPI process costs 300-600 ms to spawn. Both are paid at startup, because
  the whole point of a local voice is that "Yes?" lands immediately, and paying
  either lazily spends it on precisely the first acknowledgement.

  Smoothness. Audio is handed to the sound card in slices, and the size of
  those slices is audible -- see SLICE_MS. Synthesis also runs a sentence ahead
  of playback on its own thread, and leftover samples are carried across
  sentence boundaries, so a multi-sentence answer is one continuous stream
  rather than a series of little stalls.

  Data, not code. On the SAPI path the text crosses as a line on stdin and is
  only ever bound to a variable on the other side. Nothing Nod says is parsed
  as PowerShell, which matters because what it says is assembled from calendar
  entries and speech recognition -- a meeting titled `$(rm -r ...)` is read
  aloud, not run.

`speaking` is exposed because the mic listener needs it: without it Nod hears
its own voice through the microphone and cheerfully transcribes itself.

To hear what is installed:  python -m copilot.voice
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
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
                 voice_model: str | None = None, speaker_id: int | None = None,
                 length_scale: float | None = None) -> None:
        super().__init__(name="speaker")
        self.bus = bus
        self.voice = voice or VOICE_HINT
        self.rate = RATE if rate is None else rate
        # "piper" for the local neural voice, "sapi" for the Windows one, or
        # None to prefer piper and fall back when its model is not downloaded.
        self.engine = engine
        self.voice_model = voice_model
        # For multi-speaker models: which of them to be. en_GB-vctk-medium
        # carries 109 voices in one 75 MB file, so this is how you pick one.
        self.speaker_id = speaker_id
        # Pace. Above 1.0 is slower, and slower is the single most effective
        # knob for sounding composed rather than hurried -- a measured delivery
        # is most of what separates "an assistant" from "a text-to-speech".
        self.length_scale = length_scale
        self._piper = None
        self._syn = None
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

    def _synthesis_config(self):
        """Speaker and pace, or None to take the model's own defaults."""
        if self.speaker_id is None and self.length_scale is None:
            return None
        from piper import SynthesisConfig

        return SynthesisConfig(speaker_id=self.speaker_id,
                               length_scale=self.length_scale)

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
            self._syn = self._synthesis_config()
            list(voice.synthesize("Ready.", syn_config=self._syn))  # warm up
            self._piper = voice
            self.bus.say(f"voice: {model.stem}")
        except Exception as exc:
            self.bus.say(f"voice: piper unavailable ({type(exc).__name__})")
            self._piper_failed = True
        return self._piper

    # How much audio to hand the sound card at a time. This is the one number
    # in the file that has to be right, and getting it wrong is audible.
    #
    # It trades two things off. Small slices mean "stop" lands sooner, since
    # the cancel flag is only checked between them. Too small and the device
    # starves between writes and the speech comes out choppy -- measured on
    # this machine, 100 ms slices against a matching 100 ms device buffer
    # played a 4.48 s sentence in 4.66 s, and every one of those 180 ms of
    # overrun was a gap someone could hear.
    #
    # 250 ms slices, with the device left to pick its own buffer size, played
    # the same sentence in 4.50 s -- 20 ms over, inaudible -- while still
    # cutting off within a quarter second of being told to stop.
    SLICE_MS = 250

    def _speak_piper(self, voice, sentences: list[str]) -> None:
        """Synthesise and play, in slices small enough to stop part-way.

        SAPI could only ever be interrupted between sentences, because Speak()
        blocks until the whole string is finished. Checking the cancel flag
        between slices makes "stop" land inside a sentence instead.
        """
        import numpy as np
        import soundcard as sc

        rate = voice.config.sample_rate
        slice_frames = max(1, int(rate * self.SLICE_MS / 1000))

        # No blocksize: soundcard picks one that suits the device. Forcing it
        # to match the slice size is what caused the underruns above.
        # Synthesis runs one sentence ahead of playback, on its own thread.
        #
        # Doing it inline instead is what a first attempt looks like, and it
        # leaves a gap between every sentence while the next one is generated
        # -- about 300 ms of silence mid-answer, which sounds like stuttering
        # rather than like a pause for breath. Piper runs at roughly 14x
        # realtime, so a single sentence of lookahead is always ready before
        # it is wanted, and the queue bound keeps memory flat on long answers.
        ready: queue.Queue = queue.Queue(maxsize=2)

        def synthesise() -> None:
            try:
                for sentence in sentences:
                    if self._cancel.is_set():
                        break
                    chunks = [np.asarray(c.audio_int16_array, dtype=np.int16)
                              for c in voice.synthesize(
                                  sentence, syn_config=self._syn)]
                    if not chunks:
                        continue
                    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
                    ready.put((sentence, samples))
            except Exception:
                pass
            finally:
                ready.put(None)          # end marker, always sent

        worker = threading.Thread(target=synthesise, name="piper-synth",
                                  daemon=True)
        worker.start()

        first = True
        # Whatever was left over from the previous sentence, carried forward so
        # every write to the device is a full slice.
        #
        # Playing each sentence as its own run of slices leaves a short, ragged
        # final write at every boundary, and the device starves there: measured
        # across four sentences that cost 0.6 s of overrun, heard as a stutter
        # between them. Carrying the remainder makes the whole answer one
        # continuous stream, with a partial write only at the very end.
        carry = np.empty(0, dtype=np.float32)

        with sc.default_speaker().player(samplerate=rate) as player:
            while not self._cancel.is_set():
                try:
                    item = ready.get(timeout=30)
                except queue.Empty:
                    break
                if item is None:
                    if len(carry) and not self._cancel.is_set():
                        player.play(carry)
                    break

                sentence, samples = item
                self.current_line = sentence
                if first:
                    perf.mark("speech.start", sentence[:40])
                    first = False

                stream = np.concatenate((carry, samples)) if len(carry) else samples
                full = (len(stream) // slice_frames) * slice_frames
                for at in range(0, full, slice_frames):
                    if self._cancel.is_set():
                        break
                    player.play(stream[at:at + slice_frames])
                carry = stream[full:]

        # Drain so the producer cannot outlive this call. On an interrupt it is
        # usually blocked in ready.put() against the bounded queue; taking
        # items off frees it to notice the cancel flag and exit.
        while worker.is_alive():
            try:
                ready.get_nowait()
            except queue.Empty:
                worker.join(0.05)

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


def _voices() -> list[Path]:
    return sorted(VOICES_DIR.glob("*.onnx")) if VOICES_DIR.is_dir() else []


def audition(text: str = "", model: str | None = None) -> int:
    """Hear the installed voices, so choosing one is not guesswork.

        python -m copilot.voice                  # every voice, in turn
        python -m copilot.voice --model ryan     # just that one
        python -m copilot.voice --set ryan       # pick it and save

    Voice quality is the one thing in this project that cannot be measured --
    latency and dropouts have numbers, "does this sound right" does not. So
    this exists to put the choice in front of the person who has to listen
    to it.
    """
    from .bus import Bus

    found = _voices()
    if not found:
        print(f"No voices in {VOICES_DIR}")
        print("Download one with, from inside that folder:")
        print("    python -m piper.download_voices en_US-ryan-high")
        return 1

    if model:
        found = [p for p in found if model.lower() in p.stem.lower()] or found

    line = text or ("Yes, boss? The exchange rate is about fifty eight pesos "
                    "to the dollar today.")
    bus = Bus()
    for path in found:
        print(f"\n  {path.stem}  ({path.stat().st_size / 1e6:.0f} MB)")
        speaker = Speaker(bus, voice_model=str(path))
        if not speaker._ensure_piper():
            print("    could not load")
            continue
        speaker.say_now(line)
        time.sleep(0.4)

    print(f"\nTo keep one:  python -m copilot.voice --set <name>")
    return 0


# Southern-English male speakers in en_GB-vctk-medium, per the VCTK corpus'
# speaker metadata. Shortlisted from 109 because the brief was a composed RP
# British voice, and auditioning all of them is nobody's afternoon.
VCTK_BRITISH_MALE = {
    "p226": 95,   # Surrey
    "p227": 82,   # Cumbria
    "p232": 60,   # Southern England
    "p243": 81,   # London
    "p254": 76,   # Surrey
    "p258": 57,   # Southern England
    "p273": 19,   # Suffolk
    "p274": 10,   # Essex
}


def audition_vctk(text: str = "", length_scale: float = 1.45) -> int:
    """Hear the shortlisted British male voices inside the multi-speaker model.

    One 75 MB file holds 109 voices, so this is far and away the cheapest way
    to find a timbre worth keeping. length_scale defaults slower than the
    model's own 1.4 because an unhurried delivery is most of what makes a
    voice sound composed rather than mechanical.
    """
    from .bus import Bus

    model = VOICES_DIR / "en_GB-vctk-medium.onnx"
    if not model.exists():
        print("Download it first, from inside", VOICES_DIR)
        print("    python -m piper.download_voices en_GB-vctk-medium")
        return 1

    line = text or ("Yes, sir. The exchange rate is about fifty eight pesos "
                    "to the dollar. I have taken the liberty of checking.")
    bus = Bus()
    for name, sid in VCTK_BRITISH_MALE.items():
        print(f"\n  {name}  (speaker {sid})")
        sp = Speaker(bus, voice_model=str(model), speaker_id=sid,
                     length_scale=length_scale)
        if not sp._ensure_piper():
            print("    could not load")
            continue
        sp.say_now(line)
        time.sleep(0.5)

    print("\nTo keep one:  python -m copilot.voice --set-vctk p243")
    return 0


if __name__ == "__main__":
    import argparse
    import time

    from . import config

    ap = argparse.ArgumentParser(prog="copilot.voice")
    ap.add_argument("--model", default=None,
                    help="only audition voices matching this")
    ap.add_argument("--set", dest="pick", default=None,
                    help="save the matching voice as the one to use")
    ap.add_argument("--say", default="", help="say this instead of the sample")
    ap.add_argument("--vctk", action="store_true",
                    help="audition the British male voices in the VCTK model")
    ap.add_argument("--set-vctk", dest="set_vctk", default=None,
                    help="keep one VCTK speaker, e.g. p243")
    ap.add_argument("--pace", type=float, default=1.45,
                    help="length scale; higher is slower and more composed")
    ns = ap.parse_args()

    if ns.set_vctk:
        sid = VCTK_BRITISH_MALE.get(ns.set_vctk)
        if sid is None:
            print(f"Unknown speaker {ns.set_vctk!r}. "
                  f"Try one of: {', '.join(VCTK_BRITISH_MALE)}")
            raise SystemExit(1)
        cfg = config.load()
        cfg["voice_model"] = str(VOICES_DIR / "en_GB-vctk-medium.onnx")
        cfg["voice_speaker_id"] = sid
        cfg["voice_length_scale"] = ns.pace
        config.save(cfg)
        print(f"Nod will now speak as VCTK {ns.set_vctk} at pace {ns.pace}")
        raise SystemExit(0)

    if ns.vctk:
        raise SystemExit(audition_vctk(ns.say, ns.pace))

    if ns.pick:
        match = [p for p in _voices() if ns.pick.lower() in p.stem.lower()]
        if not match:
            print(f"No voice matching {ns.pick!r} in {VOICES_DIR}")
            raise SystemExit(1)
        cfg = config.load()
        cfg["voice_model"] = str(match[0])
        config.save(cfg)
        print(f"Nod will now speak with {match[0].stem}")
        raise SystemExit(0)

    raise SystemExit(audition(ns.say, ns.model))
