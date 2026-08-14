"""Speech recognition, locally.

This stage sits between the raw 100 ms blocks on bus.audio and the text the
rest of the pipeline reasons about. Two decisions shape it:

  It runs Whisper locally rather than through an API. Audio never leaves the
  machine, which is the claim the README makes on the privacy slide, and it
  removes a network round trip from the latency budget — only the rolling text
  window goes out over the wire, from llm.py.

  It does not decide where utterances begin and end. `Segmenter` in audio.py
  already cuts on silence with hysteresis, and putting that logic in one place
  means the tuning table in the README stays true. This thread just drains the
  queue, hands whole utterances to the model, and publishes text.

The model is loaded inside run(), not __init__. Loading `small.en` takes tens
of seconds and downloads ~500 MB the first time; doing it in the constructor
would block main() before the Qt window exists, so the user would stare at an
empty screen instead of the HUD saying "loading model".
"""

from __future__ import annotations

import queue
import threading

from .audio import SAMPLE_RATE, Segmenter
from .bus import Bus, TranscriptSegment

# Whisper trained on 30-second windows of real speech, so on near-silence or
# noise it will confidently emit whatever filled that slot in its training
# data — usually subtitle boilerplate. Cheaper to drop the known offenders here
# than to let them reach the context window and get summarised as fact.
HALLUCINATIONS = {
    "thank you.",
    "thanks for watching!",
    "thank you for watching.",
    "you",
    "bye.",
    ".",
    "[ silence ]",
    "[music]",
    "(upbeat music)",
    # Same failure in Tagalog — the sign-off that ends most Filipino uploads,
    # so it is what the model reaches for when it hears music or room tone.
    "salamat sa panonood.",
    "salamat sa panonood!",
    "maraming salamat.",
    "salamat po.",
}


class Transcriber(threading.Thread):
    """bus.audio -> faster-whisper -> bus.transcripts."""

    daemon = True

    # Below this, the decoder is telling us it heard no speech. Whisper's own
    # threshold is 0.6; we are stricter because a meeting HUD showing an
    # invented sentence is worse than one showing nothing.
    NO_SPEECH_MAX = 0.5

    def __init__(
        self,
        bus: Bus,
        model: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
    ) -> None:
        super().__init__(name="transcriber")
        self.bus = bus
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        # None means detect per utterance. Pinning it with --language is both
        # faster and steadier when you already know what you are listening to;
        # detection can flip between related languages on a short noisy chunk.
        self.language = language

    def _load(self):
        from faster_whisper import WhisperModel

        self.bus.say(f"whisper: loading {self.model_name} (first run downloads ~500MB)")
        model = WhisperModel(
            self.model_name, device=self.device, compute_type=self.compute_type
        )
        self.bus.say(f"whisper: {self.model_name} on {self.device}")
        return model

    def _decode(self, model, audio) -> str:
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=1,              # greedy: ~2x faster, and the LLM stage
                                      # infers through recognition errors anyway
            vad_filter=False,         # Segmenter already did this, on RMS
            # Without this, one bad utterance poisons the next few — Whisper
            # conditions on its own previous output and can loop.
            condition_on_previous_text=False,
        )

        kept = [
            s.text.strip()
            for s in segments
            if getattr(s, "no_speech_prob", 0.0) < self.NO_SPEECH_MAX
        ]
        text = " ".join(t for t in kept if t).strip()
        return "" if text.lower() in HALLUCINATIONS else text

    def run(self) -> None:
        # Wait for meeting mode before loading anything. Loading up front would
        # cost ~460 MB and ten seconds in agent-only sessions that never
        # transcribe a meeting at all -- which is the common case. The price is
        # that the *first* toggle takes those ten seconds; every one after is
        # instant, because the model stays resident once loaded.
        if not self.bus.idle(self.bus.meeting_on):
            return
        try:
            model = self._load()
        except Exception as exc:
            self.bus.say(f"whisper: failed to load ({exc})")
            return

        seg = Segmenter()

        while not self.bus.stop.is_set():
            if not self.bus.meeting_on.is_set():
                if not self.bus.idle(self.bus.meeting_on):
                    break
                seg = Segmenter()      # drop any half-built utterance
                continue
            try:
                block = self.bus.audio.get(timeout=0.2)
            except queue.Empty:
                continue        # no audio yet; re-check bus.stop

            utterance = seg.push(block.samples)
            if utterance is None:
                continue        # still mid-speech, or too short to bother with

            try:
                text = self._decode(model, utterance)
            except Exception as exc:
                self.bus.say(f"whisper: {type(exc).__name__}")
                continue

            if not text:
                continue

            # drop-oldest rather than put: the queue is bounded now, and a
            # transcriber blocking on a full queue would stop consuming audio,
            # which is the one thing it must never do.
            self.bus.put_drop_oldest(
                self.bus.transcripts,
                TranscriptSegment(text=text, duration=len(utterance) / SAMPLE_RATE)
            )
