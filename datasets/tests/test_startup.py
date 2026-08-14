"""Everything Nod needs at boot actually exists.

Written after a real failure that got all the way to the user. An edit spliced
two new methods into the middle of `Bus.__init__`, so the assignments below
them became unreachable code sitting after a `return`. `Bus` still imported,
still constructed, still passed `compileall` -- it simply had no `.stop`, and
every worker thread died on its first line:

    AttributeError: 'Bus' object has no attribute 'stop'

The lesson is narrow and worth keeping: importing a module proves the syntax
parses, and constructing an object proves `__init__` does not raise. Neither
proves `__init__` finished. Only touching the attributes does that.

This runs in about a second and needs no models, no network and no API key --
cheap enough that there is no excuse for skipping it before handing something
over.

    python datasets\tests\test_startup.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.bus import Bus                                    # noqa: E402

# Every attribute some other module reaches for on the bus. When a worker
# starts using a new one, it belongs here too.
BUS_ATTRS = [
    # queues
    "audio", "transcripts", "screen", "suggestions", "status",
    "speech", "heard", "mic_audio",
    # events and state
    "stop", "force_refresh", "hud_hide", "hud_rect",
    "meeting_on", "agent_on",
]
BUS_METHODS = ["put_drop_oldest", "say", "toggle_meeting", "toggle_agent",
               "idle"]


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def main() -> int:
    bad = 0

    print("Bus surface")
    bus = Bus()
    for attr in BUS_ATTRS:
        bad += check(f"bus.{attr}", hasattr(bus, attr))
    for meth in BUS_METHODS:
        bad += check(f"bus.{meth}()", callable(getattr(bus, meth, None)))

    print("\nBus behaviour")
    bus.meeting_on.clear()
    bus.toggle_meeting()
    bad += check("toggle_meeting turns on", bus.meeting_on.is_set())
    bus.toggle_meeting()
    bad += check("toggle_meeting turns off", not bus.meeting_on.is_set())

    bus.agent_on.clear()
    bus.toggle_agent()
    bad += check("toggle_agent turns on", bus.agent_on.is_set())
    bus.toggle_agent()
    bad += check("toggle_agent turns off", not bus.agent_on.is_set())
    bad += check("the two halves are independent",
                 not bus.meeting_on.is_set() and not bus.agent_on.is_set())

    stopping = Bus()
    stopping.stop.set()
    bad += check("idle() returns False on shutdown",
                 stopping.idle(stopping.agent_on, 0.01) is False)
    ready = Bus()
    ready.agent_on.set()
    bad += check("idle() returns True when flag set",
                 ready.idle(ready.agent_on, 0.01) is True)

    # Constructors only -- no thread is started, so nothing loads a model.
    print("\nWorkers construct")
    from copilot.agent import Agent
    from copilot.audio import LoopbackCapture
    from copilot.listen import CommandListener, MicCapture
    from copilot.llm import Suggester
    from copilot.context import ContextStore
    from copilot.transcribe import Transcriber
    from copilot.vision import ScreenReader
    from copilot.voice import Speaker

    b = Bus()
    speaker = Speaker(b)
    built = {
        "LoopbackCapture": lambda: LoopbackCapture(b, None),
        "Transcriber": lambda: Transcriber(b, "base", "cpu", "int8", None),
        "ScreenReader": lambda: ScreenReader(b),
        "Suggester": lambda: Suggester(b, ContextStore(120)),
        "Speaker": lambda: speaker,
        "MicCapture": lambda: MicCapture(b, None),
        "CommandListener": lambda: CommandListener(b, speaker, "base", "cpu",
                                                   "int8", None),
        "Agent": lambda: Agent(b, speaker, api_key="x", local_intent=True),
    }
    for name, make in built.items():
        try:
            make()
            bad += check(name, True)
        except Exception as exc:
            bad += check(name, False, f"{type(exc).__name__}: {exc}")

    # The echo row is only as good as the wiring behind it: the field has to
    # exist on Suggestion and _card has to fill it, or every agent card
    # silently renders without the transcript and nothing fails loudly.
    print("\nAgent cards carry the transcript")
    from copilot.bus import Suggestion
    bad += check("Suggestion has .heard", hasattr(Suggestion("x"), "heard"))

    echo_bus = Bus()
    agent = Agent(echo_bus, Speaker(echo_bus), api_key="x", local_intent=True)
    agent._heard = "play a music in brave"
    agent._card("Playing brave", ["YouTube Music"])
    card = echo_bus.suggestions.get_nowait()
    bad += check("_card fills it from _heard",
                 card.heard == "play a music in brave", f"got {card.heard!r}")
    bad += check("summariser cards leave it empty", Suggestion("x").heard == "")

    # Files that are loaded from disk at runtime rather than imported. Under
    # PyInstaller these come from sys._MEIPASS via paths.resource, and both
    # fail *quietly* if the spec ever stops bundling them: a missing
    # speaker.ps1 leaves Nod mute, and a missing dataset drops the local
    # classifier from 98.4% to 87.3% with nothing said anywhere.
    print("\nRuntime data files resolve")
    from copilot import local_intent, paths

    for rel in (("copilot", "speaker.ps1"),
                ("datasets", "commands.train.jsonl")):
        bad += check("/".join(rel), paths.resource(*rel).exists())
    shots = local_intent.shots()
    bad += check("few-shot examples load", len(shots) == 48,
                 f"got {len(shots)}, expected 48 (12 intents x 2 rows x 2 turns)")

    # The build excludes onnxruntime and sympy, ~97 MB, on the strength of every
    # transcribe() call passing vald_filter=False -- faster_whisper only imports
    # onnxruntime inside the VAD path. Turning VAD on somewhere would not fail
    # here; it would fail in the packaged build, on a tester's machine, as an
    # ImportError from a worker thread.
    print("\nThe bundle's size assumptions still hold")
    import copilot.listen
    import copilot.transcribe

    vad_on = [m.__name__ for m in (copilot.listen, copilot.transcribe)
              if "vad_filter=True" in inspect.getsource(m)]
    bad += check("no vad_filter=True (onnxruntime stays excluded)",
                 not vad_on, str(vad_on))

    # The gated loops all call bus.idle(...) -- if a rename breaks that, the
    # thread dies silently at runtime rather than here.
    print("\nGated loops reference the flags")
    import copilot.audio, copilot.listen, copilot.llm, copilot.transcribe, copilot.vision
    for mod, flag in [(copilot.audio, "meeting_on"),
                      (copilot.transcribe, "meeting_on"),
                      (copilot.vision, "meeting_on"),
                      (copilot.llm, "meeting_on"),
                      (copilot.listen, "agent_on")]:
        src = inspect.getsource(mod)
        bad += check(f"{mod.__name__.split('.')[-1]} gates on {flag}",
                     f"bus.{flag}" in src)

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
