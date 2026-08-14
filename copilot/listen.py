"""The microphone side: wake word, then command.

This is deliberately a separate path from audio.py/transcribe.py, which listen
to the *speakers*. Keeping them apart is the one non-negotiable rule of the
agent: the meeting Nod is summarising must never be able to issue it orders. If
both fed one queue, a webinar host saying "hey Nod, join the next call" would
be obeyed. Two capture threads, two queues, two transcribers.

The wake word runs through the same Whisper stack as everything else instead of
a dedicated engine like openWakeWord or Porcupine. That is a real trade -- a
purpose-built detector is lighter and catches the phrase more reliably -- but
"hey Nod" is not a phrase either ships pretrained, custom training is its own
project, and the existing Segmenter already gates on speech energy so Whisper
only runs when someone actually talks. It costs close to nothing while the room
is quiet.

Recognition is fuzzy on purpose. Whisper renders the phrase as "Hey Nod",
"Hey, nod.", "hey not", "Hey Naud", "hey node" depending on the mic and how
fast it was said, so matching the literal string would fail most of the time.

It also renders it as "Hey, none.", "Hey, Nun!" and "Hey Null.", which is a
harder problem, because those are real English words and fuzzy matching cannot
be loosened far enough to catch them without waking on ordinary speech. See
NAMES / NAMES_WEAK below -- that split is the difference between answering and
sitting there while someone repeats themselves.
"""

from __future__ import annotations

import difflib
import queue
import re
import threading
import time

import numpy as np
import soundcard as sc

from . import perf
from .audio import BLOCK_FRAMES, SAMPLE_RATE, Segmenter
from .bus import AudioBlock, Bus

_PUNCT = re.compile(r"[^a-z0-9 ]+")

# The two halves are matched separately rather than as one joined string.
# Fuzzy-matching "he nodded" against "hey nod" scores 0.75 -- close enough to
# trip a whole-phrase threshold, so "he nodded and walked away" used to wake
# it. Scoring the greeting and the name independently fixes that: "he" still
# passes as a greeting, but "nodded" is a poor match for "nod" and the pair is
# rejected.
GREETINGS = ("hey", "hi", "hay", "hello", "ey", "hei")

# Names split into two tiers, and the split is the whole point.
#
# Measured against ~/.nod/perf.log -- 18 real attempts at the wake word, logged
# from this machine's headset -- the single-tier list below caught 7. The other
# 11 were rejected while the user said "hey Nod" over and over. The misses were
# not random: the final /d/ of "Nod" goes unreleased in ordinary speech, so
# Whisper hears a nasal and writes "none", "nun", "nahn", "null". Those score
# 0.57-0.75 against "nod", nowhere near any threshold that "nodded" does not
# also clear, so no amount of tuning NAME_RATIO fixes it. The list was simply
# missing the renderings this microphone actually produces.
#
#   NAMES       not words anyone says by accident, so fuzzy matching is safe.
#   NAMES_WEAK  real English words. Matched *exactly* and only when what
#               follows is not ordinary prose -- see PROSE_NEXT. Fuzzy-matching
#               these would wake on "Ned", "kneed", "needs".
NAMES = ("nod", "not", "node", "naud", "knot", "nud", "gnawed", "nought",
         "nawd", "naht", "nawt")
NAMES_WEAK = ("none", "nun", "null", "nerd", "na", "gnome", "nut", "non",
              "norv", "nahn", "nan", "known", "noun")

# What separates "hey Nod" from "hey, none of that matters now". A weak name
# followed by one of these is being used as English, not as a name -- nobody
# opens an instruction to an assistant with "of", "was" or "because". Only weak
# names are tested against it, so a clear "hey Nod, is the build green" is
# never affected.
PROSE_NEXT = frozenset({
    "of", "that", "this", "these", "those", "but", "nor", "and", "or", "if",
    "so", "was", "were", "is", "are", "has", "had", "have", "will", "would",
    "could", "should", "then", "though", "because", "which", "whose", "whom",
    "from", "with", "about", "into", "over", "under", "after", "before",
    "at", "as", "than", "too", "very", "really", "just", "even", "also",
})

GREET_RATIO = 0.75
# 0.85, not 0.80: "nodded" scores exactly 0.80 against "node", so a looser bar
# wakes on "he nodded and walked away". Every genuine variant scores 1.0
# against its own entry, so the tighter bar costs nothing.
NAME_RATIO = 0.85
WAKE_WINDOW = 4            # only look this far in for the phrase

# After acknowledging, how long to wait for the user to *start* the actual
# instruction before deciding it was a false trigger and going quiet again.
#
# "Start" is the important word, and it used to be "finish", which is the bug
# that made Nod answer "Yes?" and then ignore what came next. The window was
# checked when the utterance had been segmented and was ready to dispatch, so
# everything in between spent it: Nod speaking the acknowledgement (~1 s), the
# user reacting (~1 s), the sentence itself (2-4 s), the 1.6 s of silence
# HANG_AWAKE waits to be sure it ended, and however far behind realtime the
# decode had fallen. From a real session in perf.log:
#
#     607.281  ack.queued        Yes, boss?
#     614.031  utterance.closed  3.40s          <- 6.75 s of an 8 s window
#
# That is a *short* command and it left 1.25 s of headroom. "play me some
# music from YouTube" does not fit, so it was dropped in silence -- no reply,
# no error, nothing in the status line. Judging the window by when speech
# started removes every term except the user's own reaction time.
COMMAND_TIMEOUT = 10.0


def normalise(text: str) -> str:
    return _PUNCT.sub("", text.lower()).strip()


def _close(word: str, options: tuple[str, ...], ratio: float) -> bool:
    return any(
        difflib.SequenceMatcher(None, word, opt).ratio() >= ratio
        for opt in options
    )


def wake_split(text: str) -> tuple[bool, str]:
    """Is this the wake word, and what was said after it?

    Returns (heard_wake, remainder). The remainder matters: people say
    "hey Nod, attend my meeting" in one breath far more often than they wait
    to be asked, and making them wait for a prompt would feel broken.
    """
    words = normalise(text).split()

    # Scan a few words in, since it often lands mid-utterance:
    # "okay so hey Nod attend my meeting".
    for i in range(min(len(words) - 1, WAKE_WINDOW)):
        if not _close(words[i], GREETINGS, GREET_RATIO):
            continue

        name, rest = words[i + 1], words[i + 2:]

        if _close(name, NAMES, NAME_RATIO):
            return True, " ".join(rest).strip()

        # Weak names: exact match only, and only when the next word is not
        # ordinary prose. "hey none." is the wake word; "hey none of that
        # matters" is someone talking.
        if name in NAMES_WEAK and (not rest or rest[0] not in PROSE_NEXT):
            return True, " ".join(rest).strip()

    return False, ""


class MicCapture(threading.Thread):
    """Pushes 100 ms mono float32 mic blocks onto bus.mic_audio."""

    daemon = True

    def __init__(self, bus: Bus, device_name: str | None = None) -> None:
        super().__init__(name="mic-capture")
        self.bus = bus
        self.device_name = device_name

    def _open(self):
        if self.device_name:
            return sc.get_microphone(self.device_name)
        return sc.default_microphone()

    def run(self) -> None:
        try:
            mic = self._open()
        except Exception as exc:
            self.bus.say(f"mic: unavailable ({exc})")
            return

        self.bus.say(f"mic: {mic.name}")
        try:
            with mic.recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK_FRAMES) as rec:
                while not self.bus.stop.is_set():
                    if not self.bus.agent_on.is_set():
                        if not self.bus.idle(self.bus.agent_on):
                            break
                        continue
                    data = rec.record(numframes=BLOCK_FRAMES)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    self.bus.put_drop_oldest(
                        self.bus.mic_audio, AudioBlock(data.astype(np.float32))
                    )
        except Exception as exc:
            self.bus.say(f"mic: stopped ({exc})")


class CommandListener(threading.Thread):
    """mic -> wake word -> acknowledgement -> command onto bus.heard."""

    daemon = True

    # A headset mic sits far closer to the mouth than a laptop's, so speech
    # lands louder and the room is quieter relative to it than Segmenter's
    # defaults assume.
    START_RMS = 0.020

    # Deliberately *lower* than START_RMS by a wide margin. A trailing "uhmmm"
    # or a quiet final syllable carries far less energy than the start of a
    # sentence, and at 0.012 those fell under the bar and counted as silence --
    # which is why Nod kept answering before the sentence was finished.
    STOP_RMS = 0.007

    # Endpointing is adaptive, because the two things being listened for want
    # opposite settings and a single value has to be wrong for one of them.
    #
    #   Idle, nothing said yet: the only thing expected is "hey Nod" -- two
    #   syllables, no reason to pause inside it. Waiting 1.6 s to be sure the
    #   user has finished saying a word that takes 0.7 s is the single biggest
    #   contributor to the gap before "Mhm?".
    #
    #   Awake, or already mid-utterance: a real instruction is being dictated
    #   and people stop to think inside one. Cutting them off costs the whole
    #   sentence, so wait.
    HANG_IDLE = 6               # 0.6 s -- just the wake word
    HANG_AWAKE = 16             # 1.6 s -- a command is being composed

    # Past this much speech while still idle, assume it is a one-breath
    # "hey Nod, attend my meeting" rather than a bare wake word, and switch to
    # the patient setting mid-utterance.
    LONG_UTTERANCE_BLOCKS = 25  # 2.5 s

    MAX_UTTERANCE = 100         # 10 s, so a long instruction is not truncated

    # Nod's own sentences run several seconds; the wake word does not. While it
    # is speaking, anything this long is its own voice in the microphone, and
    # dropping it before Whisper runs saves the decode entirely.
    ECHO_MAX_SECONDS = 3.0
    ECHO_SIMILARITY = 0.8       # decoded text vs the sentence being spoken

    # Under this, an idle utterance can only be "hey Nod" -- decode it as
    # English rather than letting language detection guess from 1.2 s of audio.
    # Above it, treat it as a one-breath instruction and let detection run.
    WAKE_MAX_SECONDS = 2.5

    # Whisper renders a thinking noise as one of these. Hearing only filler
    # means the sentence is still coming, so keep the window open instead of
    # sending "um" off to be interpreted as a command.
    FILLER = {"uh", "um", "uhm", "uhmm", "uhmmm", "hmm", "hm", "er", "ah",
              "eh", "mm", "mmm", "so", "like", "well", "okay", "ok", "and"}

    # "Stop" is the one instruction that has to work without the wake word.
    # Requiring "hey Nod, stop" to stop a five-sentence answer means saying six
    # words over the top of the thing you are trying to silence, and the log
    # shows what that costs: a bare "Stop." arriving mid-answer was dropped by
    # the talking-and-not-woken rule, so Nod carried on to the end.
    #
    # Safe without the wake word because the bar is deliberately narrow: the
    # whole utterance must be nothing but these words, it must be short, and
    # the echo check has already run. Nod's own lines are sentences, so none of
    # them decode to a bare "stop".
    STOP_WORDS = {"stop", "quiet", "shut", "up", "enough", "cancel",
                  "nevermind", "shush", "silence", "hush", "wait"}
    # Allowed to appear alongside a stop word without making it an instruction:
    # "be quiet", "stop talking", "just stop", "stop it now". Kept separate
    # from STOP_WORDS because none of these on its own means anything.
    STOP_FILLER = {"be", "please", "now", "it", "talking", "you", "just"}
    STOP_MAX_SECONDS = 2.0

    # Every acknowledgement has to be made of real words. SAPI has no idea what
    # "Mhm" is, so it spelled it out letter by letter -- "em aitch em" -- which
    # is the opposite of a relaxed grunt of acknowledgement. Written-out
    # vocalisations ("uh huh", "hmm") fail the same way.
    # Replaced per persona at construction; see copilot/persona.py.
    ACKS = ("Yes?", "Yes, boss?", "Go ahead.")

    def __init__(
        self,
        bus: Bus,
        speaker,
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        start_rms: float | None = None,
        stop_rms: float | None = None,
        persona_name: str | None = None,
    ) -> None:
        super().__init__(name="command-listener")
        self.bus = bus
        self.speaker = speaker
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        # Instance attributes shadow the class ones, and _segmenter() reads them
        # through self, so overriding here is all it takes. Exposed because the
        # defaults are measured on one close-talk headset: on a laptop's array
        # mic, speech can sit entirely below START_RMS, so no utterance ever
        # opens, so Whisper is never called, so nothing is decoded -- and every
        # diagnostic in this file is downstream of a decode. It looks exactly
        # like Nod ignoring you. See calibrate.py.
        if start_rms:
            self.START_RMS = start_rms
        if stop_rms:
            self.STOP_RMS = stop_rms
        if persona_name:
            from . import persona

            self.ACKS = persona.acks(persona_name)
        self._ack_i = 0
        self._awake_until = 0.0
        # The no-audio safety net. If nothing ever crosses START_RMS, the
        # loudest thing heard is the only evidence of why -- and it is the
        # difference between "Nod is broken" and "my microphone is too quiet,
        # and Nod told me so".
        self._peak_rms = 0.0
        self._last_utterance_at = 0.0
        self._warned_silent = False
        # When the utterance currently being built started. The awake window is
        # judged against this rather than against "now", so a long instruction
        # is not thrown away for taking a long time to say.
        self._speech_started = 0.0

    def _segmenter(self) -> Segmenter:
        seg = Segmenter()
        seg.START_RMS = self.START_RMS
        seg.STOP_RMS = self.STOP_RMS
        seg.HANG_BLOCKS = self.HANG_IDLE
        seg.MAX_UTTERANCE = self.MAX_UTTERANCE
        return seg

    def _tune_hang(self, seg: Segmenter, awake: bool) -> None:
        """Pick the silence-hang for what is being listened for right now.

        Segmenter reads HANG_BLOCKS from the instance on every push, so this
        takes effect mid-utterance -- which is the point: a wake word that
        turns out to be a whole sentence gets the patient setting as soon as it
        passes LONG_UTTERANCE_BLOCKS, rather than being cut at 0.6 s.
        """
        mid_long = len(seg._buf) >= self.LONG_UTTERANCE_BLOCKS
        seg.HANG_BLOCKS = self.HANG_AWAKE if (awake or mid_long) else self.HANG_IDLE

    def _only_filler(self, text: str) -> bool:
        words = [w for w in normalise(text).split() if w]
        return bool(words) and all(w in self.FILLER for w in words)

    # Long enough that a quiet minute at your desk does not trip it, short
    # enough that someone testing Nod for the first time hears about it while
    # they are still trying.
    SILENT_AFTER = 60.0

    def _watch_for_silence(self, samples) -> None:
        """Say something if the microphone never gets loud enough to register.

        The failure this exists for is completely silent by construction: below
        START_RMS the Segmenter never opens an utterance, so Whisper is never
        called, so there is no decode, and every other diagnostic in this file
        hangs off a decode. perf.log stays empty and the status line still says
        `ears: say 'hey Nod'`. The peak level is the only evidence, so it gets
        reported once with the number needed to act on it.
        """
        if self._warned_silent:
            return

        import numpy as np

        self._peak_rms = max(
            self._peak_rms,
            float(np.sqrt(np.mean(samples ** 2)) + 1e-9))

        if time.time() - self._last_utterance_at < self.SILENT_AFTER:
            return

        self._warned_silent = True
        perf.mark("mic.silent", f"peak={self._peak_rms:.4f}")
        if self._peak_rms < self.START_RMS:
            self.bus.say(
                f"mic: nothing loud enough yet (loudest {self._peak_rms:.4f}, "
                f"need {self.START_RMS:.3f}) — run Nod Check-up")
        else:
            self.bus.say("mic: hearing you, but nothing has been a command yet")

    def _is_stop(self, text: str) -> bool:
        """Is this nothing but a request to be quiet?

        Every word must be a stop word or one of the words that legitimately
        pad one, and at least one must actually be a stop word. "stop the music
        and play jazz" is an instruction and has to reach the agent intact.
        """
        allowed = self.STOP_WORDS | self.STOP_FILLER | self.FILLER
        words = [w for w in normalise(text).split() if w]
        return (bool(words)
                and all(w in allowed for w in words)
                and any(w in self.STOP_WORDS for w in words))

    def _decode(self, model, audio, expect_wake: bool) -> str:
        """Transcribe, choosing the language by what is expected.

        Auto-detection needs material to work with, and a 1.2 s clip of "hey
        Nod" does not provide any. Left to detect, the multilingual model
        returned "Hey, Nahn", "Hey, Nun", "Hey, Dan" and on one occasion Thai
        script -- the audio was heard and decoded correctly-ish every time, and
        the wake matcher rejected all of it. Loosening the matcher is not the
        answer, because "dan" and "na" are real words that would fire on
        ordinary speech.

        So: a short utterance while idle can only be the wake word, and the
        wake word is English. Pin it. Anything longer, or anything said while
        already awake, is a real instruction and may well be Taglish -- detect
        those.
        """
        language = self.language
        if language is None and expect_wake:
            language = "en"

        segments, _ = model.transcribe(
            audio, language=language, beam_size=1, vad_filter=False,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def _acknowledge(self) -> None:
        """Queue the acknowledgement; do not wait for it to be spoken.

        say_now blocks until SAPI finishes and holds Speaker's lock while it
        does, so calling it here stalled the listener for about a second at
        exactly the moment the user starts giving the actual instruction.
        """
        ack = self.ACKS[self._ack_i % len(self.ACKS)]
        self._ack_i += 1
        perf.mark("ack.queued", ack)
        self.bus.speech.put(ack)

    def _is_echo(self, text: str) -> bool:
        """Is this Nod hearing itself?"""
        current = getattr(self.speaker, "current_line", "") or ""
        if not current:
            return False
        return difflib.SequenceMatcher(
            None, normalise(text), normalise(current)
        ).ratio() >= self.ECHO_SIMILARITY

    def run(self) -> None:
        # Same lazy load as the meeting transcriber: agent mode may be off.
        if not self.bus.idle(self.bus.agent_on):
            return
        try:
            from faster_whisper import WhisperModel
            self.bus.say(f"ears: loading {self.model_name}")
            model = WhisperModel(self.model_name, device=self.device,
                                 compute_type=self.compute_type)
            self.bus.say("ears: say 'hey Nod'")
        except Exception as exc:
            self.bus.say(f"ears: failed to load ({exc})")
            return

        seg = self._segmenter()
        self._last_utterance_at = time.time()

        while not self.bus.stop.is_set():
            if not self.bus.agent_on.is_set():
                if not self.bus.idle(self.bus.agent_on):
                    break
                seg = self._segmenter()    # drop any half-built utterance
                self._last_utterance_at = time.time()
                continue
            try:
                block = self.bus.mic_audio.get(timeout=0.2)
            except queue.Empty:
                continue

            talking = getattr(self.speaker, "speaking", False)
            awake = time.time() < self._awake_until
            self._tune_hang(seg, awake)

            was_active = seg._active
            utterance = seg.push(block.samples)
            if not was_active and seg._active:
                # Speech just began. Remember when, because by the time this
                # utterance is decoded the window may have closed underneath a
                # user who was talking the whole time.
                self._speech_started = time.time()

            self._watch_for_silence(block.samples)

            if utterance is None:
                continue
            self._last_utterance_at = time.time()

            # The dispatch decision uses the window as it stood when the user
            # opened their mouth, not as it stands now.
            awake = self._speech_started < self._awake_until

            seconds = len(utterance) / SAMPLE_RATE
            perf.mark("utterance.closed",
                      f"{seconds:.2f}s talking={talking} awake={awake}")

            # Cheapest possible echo rejection: while Nod is speaking, anything
            # this long is one of its own sentences, and the wake word is not.
            # Dropping it here skips the Whisper decode entirely.
            if talking and seconds > self.ECHO_MAX_SECONDS:
                perf.mark("echo.dropped", "too long to be the wake word")
                continue

            # Short and idle means the only thing it can be is the wake word.
            expect_wake = not awake and seconds <= self.WAKE_MAX_SECONDS
            try:
                text = self._decode(model, utterance, expect_wake)
            except Exception as exc:
                self.bus.say(f"ears: {type(exc).__name__}")
                continue
            perf.mark("decode.done", text[:40])
            if not text:
                continue

            heard_wake, remainder = wake_split(text)

            # Second echo gate, now that there is text: if it matches what Nod
            # is saying right now, it came out of the headset, not the user.
            if talking and self._is_echo(text):
                perf.mark("echo.dropped", "matches spoken line")
                continue

            # While Nod is mid-answer only the wake word counts -- and a bare
            # "stop", which has to work without it. Anything else heard then is
            # almost certainly Nod's own voice bleeding into the mic, and
            # treating that as an instruction would have it talking to itself.
            if talking and not heard_wake:
                if seconds <= self.STOP_MAX_SECONDS and self._is_stop(text):
                    self.speaker.interrupt()
                    self.bus.say("stopped")
                    perf.mark("barge.stop", text[:40])
                    self._awake_until = 0.0
                continue

            if talking and heard_wake:
                self.speaker.interrupt()
                self.bus.say("interrupted")
                if self._is_stop(remainder):
                    # "hey Nod, stop" -- the interrupt above *was* the whole
                    # request. Sending it on would spend a classifier round
                    # trip to be told to stop, and then say a confirmation
                    # over the silence that was just asked for.
                    self._awake_until = 0.0
                elif remainder and not self._only_filler(remainder):
                    self.bus.heard.put(remainder)
                else:
                    self._awake_until = time.time() + COMMAND_TIMEOUT
                continue

            if heard_wake:
                if remainder and not self._only_filler(remainder):
                    # "hey Nod, attend my meeting" -- one breath, act on it.
                    self._awake_until = 0.0
                    self.bus.heard.put(remainder)
                else:
                    # Bare wake word, or "hey Nod, uhmm..." -- still coming.
                    self._acknowledge()
                    self._awake_until = time.time() + COMMAND_TIMEOUT
            elif awake:
                if self._only_filler(text):
                    # Thinking noise. Hold the window open rather than sending
                    # "um" off to be interpreted as an instruction.
                    self._awake_until = time.time() + COMMAND_TIMEOUT
                    continue
                self._awake_until = 0.0
                self.bus.heard.put(normalise(text))
            else:
                # Decoded cleanly and dispatched nowhere. This is the state
                # that reads as "Nod is ignoring me", and it used to leave no
                # trace at all -- nothing spoken, nothing on the status line,
                # nothing in perf.log. Most of the time it is correct (someone
                # talking near the mic is not addressing Nod), so it stays
                # quiet unless it was a near miss.
                perf.mark("ignored", text[:40])
                missed_by = self._speech_started - self._awake_until
                if self._awake_until and 0 <= missed_by < 3.0:
                    self.bus.say(f"missed by {missed_by:.1f}s: {text[:36]}")
