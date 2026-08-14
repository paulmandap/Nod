"""Working out how loud this microphone actually is.

The wake word fires on raw RMS energy, and the thresholds in `listen.py` were
measured on one specific close-talk headset:

    START_RMS = 0.020     # speech has started
    STOP_RMS  = 0.007     # speech is still going (hysteresis)

A laptop's built-in array mic sits a foot further away and typically lands well
under 0.020 for ordinary speech. When that happens the Segmenter never opens an
utterance, so Whisper is never called, so nothing is decoded, so nothing is
matched -- and every diagnostic in the app is downstream of a decode. `perf.log`
is empty. The status line says `ears: say 'hey Nod'`. There is no error anywhere,
because from the code's point of view nobody has said anything.

That is the single most likely "it doesn't work on my machine", and it is
invisible without measuring. Hence this module.

Headless on purpose: the setup dialog, the doctor and a test can all call it.
"""

from __future__ import annotations

import numpy as np

from .audio import BLOCK_FRAMES, SAMPLE_RATE

# Keep the ratio the shipped values encode: 0.007 / 0.020 = 0.35. The comments
# in listen.py explain why the stop threshold sits well under the start one -- a
# trailing "uhmmm" carries far less energy than the start of a sentence -- and
# that reasoning holds at any absolute level, so scale rather than re-derive.
HYSTERESIS = 0.35

# Below this, a "loud" sample is not speech, it is a muted or dead input.
FLOOR = 0.0015

# Speech has to be at least this much louder than the room, or the two cannot be
# told apart and no threshold will work.
MIN_HEADROOM = 2.0

# Never suggest a value outside this range. Under the low end the wake word
# triggers on room tone; over the high end you would have to shout.
MIN_START, MAX_START = 0.004, 0.050


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block ** 2)) + 1e-9)


def sample(seconds: float, device: str | None = None) -> list[float]:
    """Block-by-block RMS for `seconds` of microphone audio.

    Uses the same block size and sample rate as the live path, so the numbers
    are directly comparable to START_RMS rather than merely similar.
    """
    import soundcard as sc

    mic = sc.get_microphone(device) if device else sc.default_microphone()
    levels: list[float] = []
    blocks = max(1, int(seconds * SAMPLE_RATE / BLOCK_FRAMES))
    with mic.recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK_FRAMES) as rec:
        for _ in range(blocks):
            data = rec.record(numframes=BLOCK_FRAMES)
            if data.ndim > 1:
                data = data.mean(axis=1)
            levels.append(rms(data.astype(np.float32)))
    return levels


def _p(values: list[float], pct: float) -> float:
    return float(np.percentile(values, pct)) if values else 0.0


def suggest(quiet: list[float], loud: list[float]) -> tuple[float, float, str]:
    """Thresholds for this microphone, or a reason they could not be found.

    Returns (start_rms, stop_rms, problem). When `problem` is non-empty the
    numbers are zero and the message is written to be shown to someone who does
    not know what an RMS is.

    The 90th percentile is used for both, not the mean: what matters for the
    room is how loud its *peaks* are, since those are what would false-trigger,
    and what matters for speech is how loud the person actually gets rather than
    how much of their sample was the gap between words.
    """
    noise = _p(quiet, 90)
    speech = _p(loud, 90)

    if speech < FLOOR:
        return 0.0, 0.0, (
            "I couldn't hear anything at all. Check the microphone is not "
            "muted, and that the right one is selected.")

    if speech < noise * MIN_HEADROOM:
        return 0.0, 0.0, (
            "I couldn't tell the difference between you talking and the room. "
            "Try somewhere quieter, or move closer to the microphone.")

    # Sit above the room's peaks, and comfortably under the speech level so a
    # quiet syllable at the start of a word still opens the utterance.
    start = max(noise * 3.0, speech * 0.35)
    start = min(max(start, MIN_START), MAX_START)
    return round(start, 4), round(start * HYSTERESIS, 4), ""


def describe(start: float, stop: float, noise: float, speech: float) -> str:
    """One line for the doctor's report."""
    return (f"room {noise:.4f}, speech {speech:.4f} "
            f"-> start {start:.4f}, stop {stop:.4f}")
