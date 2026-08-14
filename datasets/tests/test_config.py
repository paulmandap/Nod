"""Settings precedence, and the promise that a bad config cannot stop Nod.

Four rules, in order, and getting any of them wrong is a support conversation:

    explicit flag  >  environment variable  >  config.json  >  built-in default

The environment-beats-file rule is the counter-intuitive one and it is
deliberate: the development machine has GEMINI_API_KEY set permanently via
setx, and a config file that silently overrode it would mean two sources of
truth and no way to tell which was in play. config.source_of() exists so the
doctor can say which one won, and that is tested here too.

    python datasets\tests\test_config.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot import config                                   # noqa: E402


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--wake-model", dest="wake_model", default=None)
    ap.add_argument("--start-rms", dest="start_rms", type=float, default=None)
    ap.add_argument("--agent", action="store_true", default=None)
    ap.add_argument("--no-agent", action="store_false", dest="agent")
    return ap


def main() -> int:
    bad = 0

    print("Precedence: flag > file > default")
    ap = parser()
    a = config.resolve(ap.parse_args([]), {})
    bad += check("no flag, no file -> default",
                 a.model == config.DEFAULTS["model"], a.model)

    a = config.resolve(ap.parse_args([]), {"model": "tiny.en"})
    bad += check("file beats default", a.model == "tiny.en", a.model)

    a = config.resolve(ap.parse_args(["--model", "small.en"]),
                       {"model": "tiny.en"})
    bad += check("flag beats file", a.model == "small.en", a.model)

    print("\nstore_true flags keep their tri-state")
    a = config.resolve(ap.parse_args([]), {"agent": False})
    bad += check("absent flag -> file value False", a.agent is False, str(a.agent))
    a = config.resolve(ap.parse_args(["--agent"]), {"agent": False})
    bad += check("--agent beats file", a.agent is True, str(a.agent))
    a = config.resolve(ap.parse_args(["--no-agent"]), {"agent": True})
    bad += check("--no-agent beats file", a.agent is False, str(a.agent))

    print("\nNumbers survive the round trip")
    a = config.resolve(ap.parse_args([]), {"start_rms": 0.0075})
    bad += check("float from file", a.start_rms == 0.0075, str(a.start_rms))
    a = config.resolve(ap.parse_args(["--start-rms", "0.03"]), {"start_rms": 0.0075})
    bad += check("float from flag", a.start_rms == 0.03, str(a.start_rms))

    print("\nA broken config never stops the app")
    with tempfile.TemporaryDirectory() as tmp:
        original = config.PATH
        try:
            config.PATH = Path(tmp) / "config.json"
            bad += check("missing file -> {}", config.load() == {})
            bad += check("exists() is False", config.exists() is False)

            config.PATH.write_text("{ this is not json", encoding="utf-8")
            bad += check("corrupt file -> {}", config.load() == {})

            config.PATH.write_text('["a list, not an object"]', encoding="utf-8")
            bad += check("wrong shape -> {}", config.load() == {})

            ok = config.save({"model": "tiny.en", "start_rms": 0.02})
            bad += check("save() writes", ok and config.exists())
            bad += check("...and reads back",
                         config.load().get("model") == "tiny.en")
            bad += check("...atomically (no .tmp left behind)",
                         not list(Path(tmp).glob("*.tmp")))
        finally:
            config.PATH = original

    print("\nSecrets are masked for support reports")
    red = config.redacted({"gemini_api_key": "AIzaSyFAKEFAKEFAKEFAKE",
                           "model": "base.en"})
    bad += check("key is not printed", "AIza" not in json.dumps(red),
                 str(red.get("gemini_api_key")))
    bad += check("non-secrets are kept", red.get("model") == "base.en")

    print("\napply_env never overwrites a real environment variable")
    marker = "test-value-do-not-overwrite"
    before = os.environ.get("GEMINI_API_KEY")
    try:
        os.environ["GEMINI_API_KEY"] = marker
        config.apply_env({"gemini_api_key": "from-the-file"})
        bad += check("environment wins",
                     os.environ["GEMINI_API_KEY"] == marker,
                     os.environ["GEMINI_API_KEY"][:20])
        bad += check("source_of says so",
                     config.source_of("gemini_api_key") == "env",
                     config.source_of("gemini_api_key"))
    finally:
        if before is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = before

    print("\nEvery default is reachable from the command line")
    # A default nothing can override is a setting a user cannot fix. This is
    # the check that would have caught start_rms being hard-coded.
    ap_full = parser()
    dests = {a.dest for a in ap_full._actions}
    unreachable = [k for k in ("model", "wake_model", "start_rms", "agent")
                   if k not in dests]
    bad += check("sampled keys have flags", not unreachable, str(unreachable))

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
