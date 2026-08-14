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


def meter(seconds: float = 20.0, device: str | None = None) -> int:
    """A live level meter, so "Nod cannot hear me" becomes a number.

        python -m copilot.calibrate

    Everything about the wake word is downstream of one comparison: is this
    block louder than START_RMS? When the answer is always no, nothing else in
    the system produces any evidence at all -- no decode, no perf marker, no
    status line -- because none of it ever runs. This prints the comparison.
    """
    import time

    from .config import DEFAULTS, load

    cfg = load()
    start = float(cfg.get("start_rms") or DEFAULTS["start_rms"])
    stop = float(cfg.get("stop_rms") or DEFAULTS["stop_rms"])
    device = device or cfg.get("mic_device")

    import soundcard as sc

    mic = sc.get_microphone(device) if device else sc.default_microphone()
    print(f"Microphone : {mic.name}")
    print(f"Triggers at: {start:.4f}   (stops below {stop:.4f})")
    print(f"\nTalk normally for {seconds:.0f} seconds. "
          f"'#' means Nod would hear you.\n")

    width = 46
    peak = 0.0
    heard = 0
    blocks = int(seconds * SAMPLE_RATE / BLOCK_FRAMES)
    with mic.recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK_FRAMES) as rec:
        for _ in range(blocks):
            data = rec.record(numframes=BLOCK_FRAMES)
            if data.ndim > 1:
                data = data.mean(axis=1)
            level = rms(data.astype(np.float32))
            peak = max(peak, level)
            over = level >= start
            heard += over
            # Log scale: speech and room noise are two orders of magnitude
            # apart, and a linear bar shows that as "nothing" and "nothing".
            filled = 0 if level <= 0 else min(
                width, int((np.log10(level) + 4) / 4 * width))
            bar = ("#" if over else "-") * filled
            print(f"\r  {level:.4f} |{bar:<{width}}| "
                  f"{'HEARD' if over else '     '}", end="", flush=True)
            time.sleep(0.0)

    print(f"\n\nLoudest: {peak:.4f}   Trigger: {start:.4f}")
    if peak < start:
        print("\n  Nod would never have heard you.")
        print(f"  Your loudest was {start / max(peak, 1e-9):.1f}x too quiet.")
        print("  Fix: run Nod, open Settings, and press Calibrate --")
        print("  or pass --start-rms {:.4f} on the command line.".format(
            max(peak * 0.35, MIN_START)))
        return 1
    pct = 100 * heard / max(blocks, 1)
    print(f"\n  Nod heard you in {pct:.0f}% of that.")
    if pct < 5:
        print("  That is very low. Consider calibrating.")
        return 1
    print("  That is healthy.")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="copilot.calibrate")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--device", default=None)
    ns = ap.parse_args()
    raise SystemExit(meter(ns.seconds, ns.device))
