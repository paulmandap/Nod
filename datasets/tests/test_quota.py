"""A rate limit must not cost you the rest of the session.

Reported symptom: "Gemini only lasted 3-5 times then Nod switched to the local
model" -- while the key was in perfect health and nowhere near its daily limit.

Two bugs, compounding:

  1. Every 429 was treated as the daily quota being gone. The free tier's
     per-minute limit is by far the more common one, and one spoken question
     can spend five requests against it (a classify call plus up to four tool
     rounds, all on the same model). Four quick questions was enough.

  2. The fallback was permanent. `fallback_local = True` was set on the first
     429 and never cleared, so a limit that expires in under a minute moved
     answering to the local model until the process was restarted -- and said
     "I'm out of API quota for today", which sent people to check their billing.

So: parse which limit was hit, rest for exactly as long as Google asks, and go
back to Gemini on its own.

    python datasets\tests\test_quota.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.agent import Agent                              # noqa: E402
from copilot.bus import Bus                                  # noqa: E402
from copilot.llm_util import QuotaExhausted, _read_quota     # noqa: E402
from copilot.voice import Speaker                            # noqa: E402

# Real shapes from the Gemini REST API.
PER_MINUTE = {
    "error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaMetric": "generativelanguage.googleapis.com/generate_requests",
                 "quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
             "retryDelay": "27s"},
        ],
    }
}

PER_DAY = {
    "error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]},
        ],
    }
}

UNPARSEABLE = {"error": {"code": 429, "message": "Quota exceeded"}}


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def agent() -> tuple[Agent, Bus]:
    bus = Bus()
    return Agent(bus, Speaker(bus), api_key="x"), bus


def drain(q) -> list[str]:
    return [q.get_nowait() for _ in range(q.qsize())]


def main() -> int:
    bad = 0

    print("Telling the two limits apart")
    per_day, retry = _read_quota(PER_MINUTE)
    bad += check("per-minute is not per-day", per_day is False, str(per_day))
    bad += check("honours the retryDelay Google sent", retry == 27.0, str(retry))

    per_day, retry = _read_quota(PER_DAY)
    bad += check("per-day is recognised", per_day is True, str(per_day))
    bad += check("...and rests for a long time", retry >= 600, str(retry))

    per_day, retry = _read_quota(UNPARSEABLE)
    bad += check("an unreadable body assumes per-minute", per_day is False,
                 str(per_day))
    bad += check("...and a short rest, not a long one", 0 < retry <= 120,
                 str(retry))

    print("\nA per-minute limit is quiet and temporary")
    a, bus = agent()
    a._rest_gemini(QuotaExhausted("too fast", per_day=False, retry_after=60))
    bad += check("Gemini is rested", a._resting() is True)
    bad += check("answers locally meanwhile",
                 a._brain_name() == "the local model", a._brain_name())
    bad += check("says nothing out loud", bus.speech.empty(),
                 "a one-minute pause is not worth interrupting for")
    bad += check("but does say so on the status line",
                 any("resting" in s for s in drain(bus.status)))

    print("\n...and it expires on its own")
    a, _ = agent()
    a._rest_gemini(QuotaExhausted("too fast", per_day=False, retry_after=0.4))
    was_resting = a._resting()
    time.sleep(0.6)
    bad += check("rested, then recovered without a restart",
                 was_resting and not a._resting())
    bad += check("back on Gemini", a._brain_name() == "Gemini", a._brain_name())

    print("\nA per-day limit is announced, once")
    a, bus = agent()
    a._rest_gemini(QuotaExhausted("out for today", per_day=True, retry_after=900))
    spoken = drain(bus.speech)
    bad += check("says it out loud", len(spoken) == 1, str(spoken))
    bad += check("...and says 'today', accurately",
                 spoken and "today" in spoken[0].lower(), str(spoken))
    a._rest_gemini(QuotaExhausted("out for today", per_day=True, retry_after=900))
    bad += check("does not repeat itself", not drain(bus.speech))

    print("\n'Use Gemini' ends a rest early")
    a, _ = agent()
    a._rest_gemini(QuotaExhausted("too fast", per_day=False, retry_after=300))
    a._do_model("gemini")
    bad += check("no longer resting", a._resting() is False)

    print("\nThe assistant paces itself now")
    a, _ = agent()
    bad += check("has a rate gate", hasattr(a, "gate"))
    allowed = sum(1 for _ in range(a.RPM_LIMIT + 5) if a.gate.allow())
    bad += check("stops at the limit within a minute",
                 allowed == a.RPM_LIMIT, f"allowed {allowed}/{a.RPM_LIMIT}")
    bad += check("limit is under the free tier's ~15/min",
                 a.RPM_LIMIT < 15, str(a.RPM_LIMIT))

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
