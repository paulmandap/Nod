"""Post-processing that makes a TTS voice sound built into the machine.

Raw Piper output is clean but flat: it sounds like a file being played, not
like a presence in the room. Three cheap, conventional stages fix most of that,
and they are the same three any broadcast voice chain uses.

    EQ            carve out what muddies speech, lift what makes it crisp
    compression   even out the level so quiet syllables still carry
    widening      put it slightly around the listener rather than in a point

Written in plain numpy on purpose. scipy is not installed and pulling it in for
three filters would add ~60 MB to a build that is already 450, so:

  * EQ is done in the frequency domain. One rfft, multiply by a gain curve,
    one irfft. That avoids a per-sample IIR loop, which in pure Python would
    cost more than the speech synthesis itself.

  * The compressor measures a block RMS envelope rather than per-sample, so
    the only Python loop runs once per 256 samples -- about 350 iterations for
    a four-second sentence, which is free.

  * Widening is mid/side. The side signal is added to one channel and
    subtracted from the other, so it cancels exactly when the two are summed:
    the result is wider on speakers and headphones, and bit-for-bit unchanged
    on a mono speaker. That matters because a laptop with one speaker is a
    completely normal way to run this.

Everything is tuned for a calm, articulate, close-mic'd assistant voice. To
hear the difference:

    python -m copilot.audio_fx --compare
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class VoiceFX:
    """The chain, with the numbers that shape the character.

    Defaults are for a composed British assistant voice: warm but not boomy,
    crisp enough to be intelligible at low volume across a room, and level
    enough that a muttered aside carries as well as a full sentence.
    """

    # --- EQ ---------------------------------------------------------------
    # Nothing useful in speech lives below here, but room rumble and the
    # synthesiser's own low-frequency wander do. Removing it is most of what
    # makes a voice sound "tight" rather than "boxy".
    highpass_hz: float = 85.0

    # The 250-400 Hz region is where speech turns muddy when there is any
    # reverb or a cheap speaker. A small cut here buys clarity that a presence
    # boost alone cannot, and it is the single most useful EQ move on a voice.
    mud_hz: float = 320.0
    mud_db: float = -2.5
    mud_q: float = 0.9

    # Consonant definition. 3-5 kHz is where "t", "k" and "s" live, and it is
    # what makes a voice sound articulate and close rather than distant.
    presence_hz: float = 4200.0
    presence_db: float = 3.0
    presence_q: float = 0.7

    # A gentle shelf on top for the sense of air that separates "recording"
    # from "in the room". Kept small: too much and sibilance becomes harsh.
    air_hz: float = 9000.0
    air_db: float = 1.5

    # --- dynamics ---------------------------------------------------------
    # Gentle and slow. The goal is that the end of a sentence is as audible as
    # the start, not that everything is the same loudness -- heavy compression
    # is exactly what makes synthetic speech sound synthetic.
    comp_threshold_db: float = -22.0
    comp_ratio: float = 3.0
    comp_attack_ms: float = 12.0
    comp_release_ms: float = 180.0

    # --- stereo -----------------------------------------------------------
    # 0 is mono. Above about 0.35 it starts to sound hollow and phasey on
    # headphones, which reads as broken rather than as spacious.
    width: float = 0.22
    # How far the side signal is displaced in time. Short enough to be heard
    # as width rather than as an echo.
    width_delay_ms: float = 11.0
    # Only widen above this. Keeping the low end centred stops the voice
    # wandering and preserves mono compatibility where it matters most.
    width_min_hz: float = 700.0

    # --- level ------------------------------------------------------------
    # Leaves headroom so the sound card never has to clip, while staying loud
    # enough to hear over a fan.
    target_peak: float = 0.89

    enabled: bool = True


def _eq_curve(freqs: np.ndarray, fx: VoiceFX) -> np.ndarray:
    """The whole EQ as one gain-per-frequency curve.

    Built directly rather than as cascaded biquads: the shapes below are the
    magnitude responses those biquads would have, and since this is applied to
    a whole sentence offline there is no reason to run them sample by sample.
    """
    gain = np.ones_like(freqs)

    # High-pass: 12 dB/octave below the corner, expressed as a magnitude.
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(freqs > 0, freqs / fx.highpass_hz, 0.0)
    gain *= ratio ** 2 / np.sqrt(1.0 + ratio ** 4)
    gain[0] = 0.0                      # kill DC outright

    # Peaking bells, as a log-normal bump around the centre frequency.
    def bell(centre: float, db: float, q: float) -> np.ndarray:
        if not db:
            return np.ones_like(freqs)
        with np.errstate(divide="ignore", invalid="ignore"):
            octaves = np.where(freqs > 0, np.log2(np.maximum(freqs, 1e-6) / centre), 0.0)
        shape = np.exp(-0.5 * (octaves * q * 2.0) ** 2)
        return 10 ** (db * shape / 20.0)

    gain *= bell(fx.mud_hz, fx.mud_db, fx.mud_q)
    gain *= bell(fx.presence_hz, fx.presence_db, fx.presence_q)

    # High shelf: rises to full gain an octave above the corner.
    if fx.air_db:
        knee = np.clip(np.log2(np.maximum(freqs, 1e-6) / fx.air_hz), -1.0, 1.0)
        shelf = (knee + 1.0) / 2.0
        gain *= 10 ** (fx.air_db * shelf / 20.0)

    return gain


def equalise(mono: np.ndarray, rate: int, fx: VoiceFX) -> np.ndarray:
    """Apply the EQ curve in the frequency domain."""
    if len(mono) < 32:
        return mono
    # Pad to avoid the circular wrap-around that plain FFT filtering causes:
    # without it the tail of a sentence bleeds onto its own beginning.
    pad = 1 << int(np.ceil(np.log2(len(mono) * 2)))
    spectrum = np.fft.rfft(mono, n=pad)
    spectrum *= _eq_curve(np.fft.rfftfreq(pad, 1.0 / rate), fx)
    return np.fft.irfft(spectrum)[: len(mono)].astype(np.float32)


def compress(mono: np.ndarray, rate: int, fx: VoiceFX,
             block: int = 256) -> np.ndarray:
    """Even out the level with a soft, slow compressor.

    The envelope is measured per block and the resulting gain is interpolated
    back up to sample rate. That is a coarser detector than a real compressor
    uses, and for gentle ratios on speech it is inaudible -- while keeping the
    only Python loop down to a few hundred iterations.
    """
    if len(mono) < block * 2:
        return mono

    blocks = len(mono) // block
    frames = mono[: blocks * block].reshape(blocks, block)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)
    level_db = 20 * np.log10(rms + 1e-12)

    # How much to pull down, before smoothing.
    over = np.maximum(level_db - fx.comp_threshold_db, 0.0)
    target_db = -over * (1.0 - 1.0 / fx.comp_ratio)

    # Attack and release as one-pole smoothers, per block.
    block_ms = 1000.0 * block / rate
    a_att = np.exp(-block_ms / max(fx.comp_attack_ms, 1e-3))
    a_rel = np.exp(-block_ms / max(fx.comp_release_ms, 1e-3))
    smoothed = np.empty_like(target_db)
    current = 0.0
    for i, wanted in enumerate(target_db):
        # Falling gain (clamping down) uses the attack constant; recovering
        # uses release. Getting these the wrong way round is what makes a
        # compressor breathe audibly.
        coeff = a_att if wanted < current else a_rel
        current = coeff * current + (1.0 - coeff) * wanted
        smoothed[i] = current

    gain = 10 ** (smoothed / 20.0)
    # Interpolate the per-block gain onto every sample so there are no steps.
    centres = np.arange(blocks) * block + block / 2.0
    per_sample = np.interp(np.arange(len(mono)), centres, gain,
                           left=gain[0], right=gain[-1])

    # Make up roughly what the compressor took, so turning it on does not just
    # make everything quieter.
    makeup = 10 ** (-np.percentile(smoothed, 20) / 20.0)
    return (mono * per_sample * makeup).astype(np.float32)


def widen(mono: np.ndarray, rate: int, fx: VoiceFX) -> np.ndarray:
    """Mono to a gently wide stereo pair, without breaking mono playback.

    L = M + S and R = M - S, so summing the two channels cancels S exactly and
    returns the original centre. A single-speaker laptop therefore hears no
    difference at all, which is the property that makes this safe to leave on.
    """
    if fx.width <= 0:
        return np.stack([mono, mono], axis=1)

    delay = max(1, int(rate * fx.width_delay_ms / 1000.0))
    shifted = np.concatenate([np.zeros(delay, dtype=np.float32), mono[:-delay]])

    # High-pass the side signal so only the upper register is spread. Reuses
    # the EQ path with everything but the high-pass switched off.
    side_fx = VoiceFX(highpass_hz=fx.width_min_hz, mud_db=0.0,
                      presence_db=0.0, air_db=0.0)
    side = equalise(shifted, rate, side_fx) * fx.width

    left = mono + side
    right = mono - side
    return np.stack([left, right], axis=1).astype(np.float32)


def normalise(audio: np.ndarray, target_peak: float) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 1e-6:
        return audio
    return (audio * (target_peak / peak)).astype(np.float32)


def process(mono: np.ndarray, rate: int, fx: VoiceFX | None = None) -> np.ndarray:
    """The whole chain. Mono in, stereo (frames x 2) out.

    Order matters and is the conventional one: EQ before compression, so the
    compressor reacts to the tone you actually want rather than to mud it is
    about to remove; widening last, so the side signal is derived from the
    finished sound.
    """
    fx = fx or VoiceFX()
    if not fx.enabled:
        return np.stack([mono, mono], axis=1)

    out = equalise(mono.astype(np.float32), rate, fx)
    out = compress(out, rate, fx)
    out = widen(out, rate, fx)
    return normalise(out, fx.target_peak)


def from_config(cfg: dict) -> VoiceFX:
    """Build the chain from saved settings, ignoring anything unrecognised."""
    fx = VoiceFX()
    for key, value in (cfg.get("voice_fx") or {}).items():
        if hasattr(fx, key) and value is not None:
            setattr(fx, key, value)
    return fx


if __name__ == "__main__":
    import argparse
    import time
    from pathlib import Path

    from . import config
    from .bus import Bus
    from .voice import VOICES_DIR, Speaker

    ap = argparse.ArgumentParser(prog="copilot.audio_fx")
    ap.add_argument("--compare", action="store_true",
                    help="say the same line with the chain off, then on")
    ap.add_argument("--say", default="Yes, sir. I have taken the liberty of "
                                     "checking the exchange rate.")
    ns = ap.parse_args()

    config.apply_env()
    cfg = config.load()
    model = cfg.get("voice_model")
    if not model:
        found = sorted(VOICES_DIR.glob("*.onnx"))
        model = str(found[0]) if found else None
    if not model:
        print(f"No voice model in {VOICES_DIR}")
        raise SystemExit(1)

    bus = Bus()
    for label, enabled in (("without processing", False), ("with processing", True)):
        if not ns.compare and not enabled:
            continue
        print(f"\n  {label}")
        sp = Speaker(bus, voice_model=model,
                     speaker_id=cfg.get("voice_speaker_id"),
                     length_scale=cfg.get("voice_length_scale"))
        sp.fx = VoiceFX(enabled=enabled)
        if not sp._ensure_piper():
            print("    could not load the voice")
            raise SystemExit(1)
        sp.say_now(ns.say)
        time.sleep(0.6)
    raise SystemExit(0)
