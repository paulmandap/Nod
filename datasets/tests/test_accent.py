"""Philippine English as a phoneme transformation, checked without a speaker.

The accent is applied between phonemisation and synthesis, which means it can
be tested exactly: feed it the IPA espeak produces for a sentence and assert on
the IPA that comes out. No audio, no model, no listening.

The important safety property is the last block. A substitution that produces a
symbol the model has never seen becomes an unknown id, and an unknown id is
silence -- a word that vanishes mid-sentence is far worse than a word said with
the wrong accent. So unknown targets must leave the original alone.

    python datasets\tests\test_accent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot import accent                                   # noqa: E402

# Real espeak output for "I think that the computer is ready, sir."
THINK = list("θˈɪŋk")
THAT_THE = list("ðætðə")
COMPUTER = list("kəmpjˈuːɾɚɹ")
IS = list("ɪz")
SIR = list("sˌɜː")

# Everything the shipped voices can actually say. Deliberately a superset of
# what the substitutions target, so the "unknown target" test below has to opt
# out explicitly rather than passing by accident.
ALLOWED = set("abcdefghijklmnopqrstuvwxyzæðɐəɚɛɜɪŋɹɾʃʒθʊʌːˈˌ ,.?!")


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def shift(phonemes, name="ph", allowed=ALLOWED):
    return "".join(accent.apply(phonemes, accent.get(name), allowed))


def main() -> int:
    bad = 0
    ph = accent.get("ph")

    print("The features that carry the accent")
    bad += check("think -> tink", shift(THINK) == "tˈɪŋk", shift(THINK))
    bad += check("that the -> dat da", shift(THAT_THE) == "datda",
                 shift(THAT_THE))
    bad += check("is -> iss", shift(IS) == "ɪs", shift(IS))
    bad += check("sir -> ser (rhotic, no long schwa)",
                 shift(SIR) == "sˌɛː", shift(SIR))
    got = shift(COMPUTER)
    bad += check("computer keeps its t (no flapping)",
                 "ɾ" not in got and "t" in got, got)
    bad += check("...and ends in a consonantal r", "ɛɹ" in got, got)
    bad += check("...with no schwa left", "ə" not in got, got)

    print("\nWhat is deliberately NOT done")
    # f/p and v/b merging are real features of some speakers and the fastest
    # way to turn an accent into an impression of one. Off unless asked for.
    bad += check("f is left alone by default", shift(list("fˈɪfti")).count("f") == 2,
                 shift(list("fˈɪfti")))
    bad += check("v is left alone by default", shift(list("vɛɹi")).startswith("v"),
                 shift(list("vɛɹi")))
    bad += check("...but both are available",
                 accent.Accent(fp_merger=True).table().get("f") == "p")

    print("\nThe lighter variant is genuinely lighter")
    light = accent.get("ph-light")
    bad += check("still stops TH", shift(THINK, "ph-light") == "tˈɪŋk")
    bad += check("but keeps vowel reduction",
                 "ə" in shift(COMPUTER, "ph-light"), shift(COMPUTER, "ph-light"))
    bad += check("fewer changes than full ph",
                 len(light.table()) < len(ph.table()),
                 f"{len(light.table())} vs {len(ph.table())}")

    print("\nTurning it off means off")
    for name in ("neutral", "none"):
        bad += check(f"{name!r} resolves to no accent", accent.get(name) is None)
    bad += check("None resolves to no accent", accent.get(None) is None)
    bad += check("an unknown name does not crash", accent.get("klingon") is None)

    print("\nA symbol the model cannot say is never introduced")
    # This is the property that prevents silent words. With ɹ excluded, the
    # rhotic rule would produce an unsayable phoneme, so it must not fire.
    without_r = ALLOWED - {"ɹ"}
    got = "".join(accent.apply(SIR, ph, without_r))
    bad += check("rhotic rule skipped when its target is missing",
                 "ɹ" not in got, got)
    bad += check("...and the original survives instead", "ɜ" in got or "ɛ" in got,
                 got)
    # TH-stopping targets t, which is always present, so it should still apply.
    bad += check("unrelated rules still fire",
                 "".join(accent.apply(THINK, ph, without_r)).startswith("t"))

    print("\nNothing is lost or duplicated")
    for name in ("ph", "ph-light"):
        out = accent.apply(COMPUTER, accent.get(name), ALLOWED)
        bad += check(f"{name}: output is non-empty and plausible",
                     len(out) >= len(COMPUTER) - 1, f"{len(COMPUTER)} -> {len(out)}")

    print("\nIt describes itself for the tuning CLI")
    bad += check("describe lists active features",
                 "TH-stopping" in accent.describe(ph))
    bad += check("...and a bare accent lists fewer",
                 len(accent.describe(light)) < len(accent.describe(ph)))

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
