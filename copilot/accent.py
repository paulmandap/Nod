"""Philippine English, as a transformation of the phonemes rather than a voice.

There is no Filipino or Philippine-English voice in Piper -- the whole catalogue
is en_GB and en_US, plus one Indonesian. The usual answers to that are both bad:
feeding English text to a Tagalog model mangles it, because Tagalog spelling
rules read English words as nonsense; and training a new voice needs hours of
recorded speech that would have to come from somebody.

But an accent is not a timbre. It is a systematic substitution of sounds, and
Piper exposes exactly the layer where those live:

    voice.phonemize(text)          -> IPA phonemes, per sentence
    voice.phonemes_to_ids(...)     -> model inputs
    voice.phoneme_ids_to_audio(..) -> audio

So the accent is applied in between: take an American English voice, rewrite
its phonemes the way a Filipino speaker of fluent English produces them, and
synthesise that. No dataset, no cloning, no training -- and it is inspectable
and adjustable, which a trained voice is not.

The features below are the well-documented ones, and the choice of *which* to
apply is the whole design. Aim for an educated, fluent speaker, which is what
was asked for. Overshooting produces a caricature, so the two features most
associated with broad stereotype -- /f/ to /p/ and /v/ to /b/ -- are off by
default and stay off unless deliberately switched on.

    python -m copilot.accent --demo
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- the individual sound changes -------------------------------------------
#
# Each maps one IPA symbol espeak produces for English onto what a Philippine
# English speaker produces instead. Every target is checked against the model's
# own phoneme table before use, so a voice that lacks a symbol degrades to
# leaving that sound alone rather than producing silence.

# TH-stopping. The single most recognisable feature, and the one that costs
# nothing in intelligibility: "think" becomes "tink", "that" becomes "dat".
TH_STOPPING = {"θ": "t", "ð": "d"}

# Philippine English is rhotic -- American-influenced, unlike British English --
# and uses a full consonantal r where American English has an r-coloured vowel.
# "sir" is "ser", "computer" ends "-ter" rather than "-tuh".
RHOTIC = {"ɚ": "ɛɹ", "ɝ": "ɛɹ", "ɜ": "ɛ"}

# No intervocalic flapping. American English turns the t in "computer" into a
# quick tap; Philippine English keeps a clean t, which is a strong marker and
# also makes the speech sound more careful -- helpful for an assistant.
NO_FLAP = {"ɾ": "t"}

# Final devoicing of /z/. "is" -> "iss", "pesos" -> "pesoss".
Z_DEVOICING = {"z": "s"}

# No vowel reduction. This is the big rhythmic one: English compresses
# unstressed syllables to a schwa, and Philippine English does not, which is
# most of why it sounds syllable-timed rather than stress-timed. Mapping the
# schwa to a full low vowel approximates that without needing to know the
# spelling each schwa came from.
NO_REDUCTION = {"ə": "a", "ɐ": "a"}

# The TRAP vowel, lowered. "that" moves from /ðæt/ toward /dat/.
TRAP_LOWERING = {"æ": "a"}

# Off by default. These are real features of some speakers, but applying them
# to everything is where an accent becomes an impression of one.
FP_MERGER = {"f": "p"}
VB_MERGER = {"v": "b"}


@dataclass
class Accent:
    """Which sound changes to apply, and how strongly."""

    name: str = "ph"

    th_stopping: bool = True
    rhotic: bool = True
    no_flap: bool = True
    z_devoicing: bool = True
    no_reduction: bool = True
    trap_lowering: bool = True

    # Deliberately off. See the note above.
    fp_merger: bool = False
    vb_merger: bool = False

    # Philippine English is syllable-timed, so the pace is steadier and a touch
    # slower than the American original. Applied by voice.py, not here.
    length_scale: float = 1.35

    def table(self) -> dict[str, str]:
        """The full substitution map for these settings."""
        table: dict[str, str] = {}
        for enabled, part in (
            (self.th_stopping, TH_STOPPING),
            (self.rhotic, RHOTIC),
            (self.no_flap, NO_FLAP),
            (self.z_devoicing, Z_DEVOICING),
            (self.no_reduction, NO_REDUCTION),
            (self.trap_lowering, TRAP_LOWERING),
            (self.fp_merger, FP_MERGER),
            (self.vb_merger, VB_MERGER),
        ):
            if enabled:
                table.update(part)
        return table


NEUTRAL = Accent(name="neutral", th_stopping=False, rhotic=False, no_flap=False,
                 z_devoicing=False, no_reduction=False, trap_lowering=False,
                 length_scale=1.0)

ACCENTS = {
    "ph": Accent(),
    "filipino": Accent(),
    # A lighter touch: the two clearest features only, for someone who wants a
    # hint of it rather than the full set.
    "ph-light": Accent(name="ph-light", no_reduction=False, trap_lowering=False,
                       z_devoicing=False, length_scale=1.25),
    "neutral": NEUTRAL,
    "none": NEUTRAL,
}


def get(name: str | None) -> Accent | None:
    """The named accent, or None to leave pronunciation alone."""
    if not name:
        return None
    found = ACCENTS.get(name.lower())
    return None if found is None or found is NEUTRAL else found


def apply(phonemes: list[str], accent: Accent,
          allowed: set[str] | None = None) -> list[str]:
    """Rewrite one sentence's phonemes.

    `allowed` is the model's own phoneme table. Any substitution that would
    produce a symbol the model has never seen is skipped, because an unknown id
    is silence -- a missing sound is far worse than an unmodified one.

    Substitutions may expand to more than one phoneme (ɚ becomes ɛɹ), so this
    builds a new list rather than mapping in place.
    """
    table = accent.table()
    if not table:
        return phonemes

    out: list[str] = []
    for symbol in phonemes:
        replacement = table.get(symbol)
        if replacement is None:
            out.append(symbol)
            continue
        parts = list(replacement)
        if allowed is not None and any(p not in allowed for p in parts):
            out.append(symbol)          # model cannot say it; leave it be
            continue
        out.extend(parts)
    return out


def describe(accent: Accent) -> str:
    """What is actually being changed, for the tuning CLI and the doctor."""
    active = [name for name, on in (
        ("TH-stopping (think -> tink)", accent.th_stopping),
        ("rhotic r (sir -> ser)", accent.rhotic),
        ("no t-flapping (computer)", accent.no_flap),
        ("z devoicing (is -> iss)", accent.z_devoicing),
        ("no vowel reduction", accent.no_reduction),
        ("trap lowering (that -> dat)", accent.trap_lowering),
        ("f/p merger", accent.fp_merger),
        ("v/b merger", accent.vb_merger),
    ) if on]
    return ", ".join(active) or "none"


if __name__ == "__main__":
    import argparse
    import time
    from pathlib import Path

    from . import config
    from .bus import Bus
    from .voice import VOICES_DIR, Speaker

    ap = argparse.ArgumentParser(prog="copilot.accent")
    ap.add_argument("--accent", default="ph",
                    help=f"one of: {', '.join(sorted(ACCENTS))}")
    ap.add_argument("--compare", action="store_true",
                    help="say the line unaccented first, then accented")
    ap.add_argument("--say", default="I think that the computer is ready, sir. "
                                     "The exchange rate is about fifty pesos.")
    ap.add_argument("--model", default=None, help="which voice to speak with")
    ap.add_argument("--phonemes", action="store_true",
                    help="print the phonemes instead of speaking")
    ns = ap.parse_args()

    config.apply_env()
    cfg = config.load()
    model = ns.model or cfg.get("voice_model")
    if not model or "vctk" in str(model):
        # Philippine English is rhotic and American-influenced, so a US voice
        # is a much better starting point than a British one.
        us = sorted(VOICES_DIR.glob("en_US-*.onnx"))
        model = str(us[0]) if us else model
    if not model:
        print(f"No voice model in {VOICES_DIR}")
        raise SystemExit(1)

    accent = get(ns.accent)
    print(f"  voice  : {Path(model).stem}")
    print(f"  accent : {ns.accent} -> {describe(accent) if accent else 'none'}")

    if ns.phonemes:
        from piper import PiperVoice

        voice = PiperVoice.load(model)
        allowed = set(voice.config.phoneme_id_map)
        for sentence in voice.phonemize(ns.say):
            print("\n  before:", " ".join(sentence))
            if accent:
                print("  after :", " ".join(apply(sentence, accent, allowed)))
        raise SystemExit(0)

    bus = Bus()
    for label, use in (("without the accent", None), (f"with {ns.accent}", accent)):
        if not ns.compare and use is None:
            continue
        print(f"\n  {label}")
        sp = Speaker(bus, voice_model=model,
                     length_scale=(use.length_scale if use else 1.0))
        sp.accent = use
        if not sp._ensure_piper():
            print("    could not load the voice")
            raise SystemExit(1)
        sp.say_now(ns.say)
        time.sleep(0.6)
    raise SystemExit(0)
