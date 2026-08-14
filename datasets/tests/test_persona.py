"""The persona is a real switch, and the plain one is genuinely unchanged.

Splicing a register into three prompts is the kind of change that works when
you test it and quietly stops working when someone reformats a docstring. Two
things are worth pinning:

  * `butler` actually reaches all three places it has to -- the classifier's
    reply rules, the answerer's register, and the acknowledgements the listener
    speaks. Missing any one leaves Nod half in character, which is worse than
    not being in character at all.

  * `plain` is byte-for-byte what it always was. The persona was added as an
    option, not as a redesign, and anyone who never asks for it should not be
    able to tell it exists.

    python datasets\tests\test_persona.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot import agent, brain, persona                    # noqa: E402


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def main() -> int:
    bad = 0

    print("Both personas exist and are distinct")
    bad += check("plain and butler are registered",
                 set(persona.PROFILES) == {"plain", "butler"},
                 str(sorted(persona.PROFILES)))
    bad += check("their reply rules differ",
                 persona.reply_instructions("plain")
                 != persona.reply_instructions("butler"))
    bad += check("their acknowledgements differ",
                 persona.acks("plain") != persona.acks("butler"))

    print("\nbutler reaches all three prompts")
    classifier = agent.system_prompt("butler")
    answerer = brain.system_prompt("plain", "butler")
    bad += check("classifier carries the register",
                 "senior British assistant" in classifier)
    bad += check("answerer carries the register",
                 "measured, precise" in answerer)
    bad += check("acknowledgements changed",
                 "Sir?" in persona.acks("butler"))

    print("\nplain is untouched")
    plain_classifier = agent.system_prompt("plain")
    bad += check("classifier keeps the original wording",
                 "Sound like a competent assistant, not a chatbot"
                 in plain_classifier)
    bad += check("no butler wording leaks in",
                 "senior British assistant" not in plain_classifier)
    bad += check("answerer adds nothing",
                 brain.system_prompt("plain", "plain")
                 == brain.system_prompt("plain", None))

    print("\nAn unknown persona falls back rather than breaking")
    bad += check("unknown name -> plain",
                 persona.get("nonsense") is persona.PROFILES["plain"])
    bad += check("None -> plain", persona.get(None) is persona.PROFILES["plain"])
    bad += check("agent.system_prompt survives it",
                 "competent assistant" in agent.system_prompt("nonsense"))

    print("\nThe module-level SYSTEM other files import still works")
    # datasets/benchmark.py and copilot/local_intent.py both do
    # `from copilot.agent import SYSTEM`, so it has to stay a plain string.
    bad += check("agent.SYSTEM is a non-empty string",
                 isinstance(agent.SYSTEM, str) and len(agent.SYSTEM) > 500)
    bad += check("...and is the plain persona",
                 agent.SYSTEM == agent.system_prompt("plain"))

    print("\nThe template has no stray braces to break .format()")
    # A literal { in the prompt would raise KeyError at import time, which is
    # a spectacular way to fail on someone else's machine.
    bad += check("every persona formats cleanly",
                 all(agent.system_prompt(n) for n in ("plain", "butler", "x")))

    print("\nStyle and persona are independent axes")
    for style in ("kid", "plain", "technical"):
        prompts = {p: brain.system_prompt(style, p) for p in ("plain", "butler")}
        bad += check(f"style={style} works with both",
                     prompts["plain"] != prompts["butler"]
                     and all(len(v) > 500 for v in prompts.values()))

    print("\nThe vocal profile is documented alongside the writing")
    bad += check("VOCAL_PROFILE mentions the accent",
                 "Received Pronunciation" in persona.VOCAL_PROFILE)
    bad += check("...and the pace", "length_scale" in persona.VOCAL_PROFILE)

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
