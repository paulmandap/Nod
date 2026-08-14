"""The LLM stage, on Gemini.

Drop-in replacement for the Anthropic version — same class name, same queue
contract, so nothing else in the project changes.

Three things are different from the Claude version, and all three matter:

  Thinking is off. Gemini 2.5+ models reason before answering by default, which
  is great for hard problems and terrible for a HUD. `thinkingBudget: 0` buys
  back roughly a second.

  The schema is enforced. Gemini takes a `responseSchema`, so we get valid JSON
  from the API instead of asking nicely in the prompt and parsing hopefully.

  We rate-limit ourselves. The free tier is the binding constraint, not latency
  — see RPM_LIMIT below.

Set GEMINI_API_KEY in your environment.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import requests

from . import perf, workctx
from .bus import Bus, Suggestion
from .context import ContextStore
from .llm_util import QuotaExhausted, post_gemini

# Flash-Lite is the right pick here: the task is extraction and compression,
# not reasoning, and it carries the highest requests-per-minute allowance on
# the free tier. Swap to the full Flash model if the lines come out flat.
# gemini-2.5-flash-lite was the original default and is now closed to new API
# keys -- it returns 404 "no longer available to new users", which on a fresh
# machine looks like a broken install rather than a retired model.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

SYSTEM = """You assist someone who is listening to something live right now — a \
meeting, a video, a lecture, a podcast, a stream — and needs to process what \
they just heard. Your output is displayed in a small overlay they glance at for \
about one second, so brevity is not a style preference — anything long is \
unreadable and therefore useless.

You receive a rolling transcript of roughly the last two minutes of audio and, \
when available, text read off the screen. The transcript comes from speech \
recognition and will contain errors; infer through them rather than quoting \
them. It is a moving window with no start and no end — never remark on missing \
context or on the transcript cutting off, just work with what is in front of you.

The transcript is the primary signal: it is what they are actually listening \
to. Screen text is captured from the whole display, so it is full of things \
they are not attending to — sidebars, tabs, chat, other windows, video titles \
they never clicked. Use it only to pin down names, spellings and figures the \
transcript garbled. If the two disagree about the subject, the transcript wins. \
Never describe what is on screen instead of what is being said. When there is \
no transcript at all, then and only then summarise the screen.

Your default job is to say what the last two minutes were actually about, so \
someone who looked away or lost the thread can rejoin in one glance.

You may also receive <my_work_context>: notes the user keeps about their own \
work. It is the only thing here that is reliably true — they wrote it — but it \
may be stale or irrelevant to this meeting. It exists so that when someone asks \
them a question, you can suggest answers grounded in what they are actually \
doing, rather than in whatever the room happened to say.

Choose kind by what would help most at this exact moment:
  summary  what is being covered right now, for someone catching up
  term     jargon, an acronym, or a name was used without explanation
  reply    a question was put to them, or a decision is waiting on them —
           only when they are a participant, never for recorded content

Write lead as the thing itself, not a description of it, and never refer to the \
speaker, the video, or the meeting as an entity — state the content directly:
  "Q3 launch slipped to November"        not  "They discussed the timeline."
  "Attention replaced RNNs by removing   not  "The video explains transformers."
   sequential dependency"

For kind=reply the two fields do different jobs, and this is the case where \
getting it right matters most — the user is being looked at, waiting, with \
seconds to respond:

  lead    the question, compressed to what was actually asked.
          "Status of the batch layer?"   not  "Your manager asked you a question."

  points  up to three things they could say back, each one a sentence they
          could speak aloud verbatim. Not topics, not advice about how to
          answer — the answer itself.
            "Batch layer's done; blocked on the vendor audit"
          not
            "Provide an update on your progress"

Every answer you suggest must be traceable to <my_work_context> or to the \
transcript. You are writing words that will be said aloud, in a real meeting, \
by someone trusting you — and then believed and acted on by everyone else in \
the room. There is no recovery from a confident wrong answer.

So: if <my_work_context> is absent, or contains nothing about what was asked, \
then you do not know the status and neither does the user's overlay. In that \
case every point must be a holding answer or a question back — never an \
assertion about progress, completion, timelines, testing, or confidence.

  no context, correct:                    no context, forbidden:
    "Say you'll confirm and follow up"      "It's on track for this sprint"
    "Ask which part they need first"        "Testing is underway"
    "Say you'll check and reply today"      "We expect no delays"

The forbidden column is not a style problem. Those sentences invent facts about \
work you cannot see, and the user will read them off the screen believing you \
got them from somewhere.

Keep lead under 75 characters and each point under 70 — measured against the \
overlay's actual font and width, past which the panel silently truncates with \
an ellipsis and the last words are simply lost. Give at most three \
points — the overlay discards any beyond that, so a fourth is content thrown \
away. Two good points beat three padded ones, and zero is fine when the lead \
already says it.

If the last 30 seconds contain nothing worth surfacing, return an empty lead."""

# Gemini validates against this, so the parse below can't fail on shape.
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["summary", "term", "reply"]},
        "lead": {"type": "STRING"},
        "points": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["kind", "lead", "points"],
    "propertyOrdering": ["kind", "lead", "points"],
}


class DailyBudget:
    """A hard cap on summariser calls per day, persisted across restarts.

    The free tier allows 500 requests per day per model. At MIN_INTERVAL the
    summariser can issue 7.5 a minute, so a long meeting spends the entire day's
    allowance in about an hour -- and the first thing you notice is not that the
    HUD went quiet, but that the assistant has stopped answering, because they
    draw on the same account.

    Kept in a file rather than memory because the interesting case is exactly
    the one where Nod has been restarted a dozen times during a working day.
    """

    def __init__(self, limit: int, path: Path | None = None) -> None:
        self.limit = limit
        self.path = path or (Path(os.environ.get("NOD_HOME",
                                                 Path.home() / ".nod"))
                             / "summary-budget.json")

    def _load(self) -> tuple[str, int]:
        today = dt.date.today().isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return today, int(data.get("count", 0))
        except Exception:
            pass
        return today, 0

    def take(self) -> bool:
        today, count = self._load()
        if count >= self.limit:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"date": today, "count": count + 1}),
                                 encoding="utf-8")
        except Exception:
            pass          # a budget we cannot persist should not stop the app
        return True

    @property
    def spent(self) -> int:
        return self._load()[1]


class RateGate:
    """Client-side sliding window, so we get told 'no' by our own code.

    Hitting a 429 costs a full round trip and returns nothing. Refusing to send
    costs nothing. On a free key this is the difference between a HUD that goes
    quiet halfway through the demo and one that doesn't.
    """

    def __init__(self, per_minute: int) -> None:
        self.limit = per_minute
        self._sent: deque[float] = deque()

    def allow(self) -> bool:
        now = time.time()
        while self._sent and now - self._sent[0] > 60.0:
            self._sent.popleft()
        if len(self._sent) >= self.limit:
            return False
        self._sent.append(now)
        return True


class Suggester(threading.Thread):
    daemon = True

    # Free-tier ceilings move around and differ per model and project — check
    # the rate-limit panel in AI Studio for the project behind your key and set
    # this a little under whatever it says.
    RPM_LIMIT = 8

    # Of the 500-per-day free allowance, the summariser may spend 300. The rest
    # is held back for the assistant, which is the half you notice losing.
    DAILY_LIMIT = 300

    MIN_INTERVAL = 8.0       # 7.5 req/min at full tilt, comfortably under RPM_LIMIT
    LULL_SECONDS = 1.2       # wait for a gap in speech
    MIN_NEW_CHARS = 220      # enough new material to be worth a request
    MAX_INTERVAL = 40.0      # ...but don't go silent forever

    def __init__(self, bus: Bus, ctx: ContextStore, api_key: str | None = None) -> None:
        super().__init__(name="suggester")
        self.bus = bus
        self.ctx = ctx
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.gate = RateGate(self.RPM_LIMIT)
        self.budget = DailyBudget(self.DAILY_LIMIT)
        self._warned_budget = False
        self.session = requests.Session()
        self._last_call = 0.0
        self._last_input = 0.0
        self._backoff_until = 0.0

    def note_input(self) -> None:
        self._last_input = time.time()

    def _should_fire(self) -> bool:
        now = time.time()
        if now < self._backoff_until:
            return False
        if self.bus.force_refresh.is_set():
            self.bus.force_refresh.clear()
            return True
        if now - self._last_call < self.MIN_INTERVAL:
            return False
        if now - self._last_input < self.LULL_SECONDS:
            return False        # someone is mid-sentence; let them finish
        if self.ctx.pending_chars >= self.MIN_NEW_CHARS:
            return True
        return now - self._last_call > self.MAX_INTERVAL and self.ctx.pending_chars > 0

    def _request(self, prompt: str) -> dict | None:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 300,
                "responseMimeType": "application/json",
                "responseSchema": SCHEMA,
                # The single most important line in this file for latency.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        # Shared helper, which also retries without thinkingConfig on a 400.
        # This path lacked that retry, so pointing GEMINI_MODEL at a
        # 3.5-generation model left the summariser 400ing forever with only
        # "gemini: HTTP 400" to go on.
        try:
            payload = post_gemini(self.session, ENDPOINT, self.api_key, body,
                                  timeout=12)
        except QuotaExhausted:
            # Back off hard — retrying immediately just burns the window.
            self._backoff_until = time.time() + 60
            self.bus.say("gemini: rate limited, pausing 60s")
            return None
        except Exception as exc:
            self.bus.say(f"gemini: {exc}")
            return None

        cands = payload.get("candidates") or []
        if not cands:
            # Usually a safety block on the prompt side.
            self.bus.say("gemini: no candidate returned")
            return None

        cand = cands[0]
        if cand.get("finishReason") not in (None, "STOP"):
            self.bus.say(f"gemini: finish={cand.get('finishReason')}")
            return None

        parts = cand.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return json.loads(text) if text.strip() else None

    def run(self) -> None:
        if not self.api_key:
            self.bus.say("gemini: GEMINI_API_KEY not set")
            return
        self.bus.say(f"gemini: {MODEL}")

        while not self.bus.stop.is_set():
            if not self.bus.meeting_on.is_set():
                if not self.bus.idle(self.bus.meeting_on):
                    break
                continue
            time.sleep(0.3)
            if not self._should_fire():
                continue
            if not self.gate.allow():
                continue        # our own limiter said no; try again shortly
            if not self.budget.take():
                if not self._warned_budget:
                    self.bus.say(f"summary: daily budget spent "
                                 f"({self.DAILY_LIMIT}), saving quota for the assistant")
                    self._warned_budget = True
                continue

            transcript, screen = self.ctx.snapshot()
            if not transcript and not screen:
                continue
            self._last_call = time.time()

            parts = [f"<transcript>\n{transcript}\n</transcript>"]
            if screen:
                parts.append(f"<shared_screen>\n{screen}\n</shared_screen>")

            # Costs no extra request -- it rides on the one already being made,
            # which is what makes answer-assist affordable on a free key.
            work = workctx.read()
            if work:
                parts.append(f"<my_work_context>\n{work}\n</my_work_context>")

            perf.mark("summary.request", f"kind-pending work={bool(work)}")
            try:
                data = self._request("\n\n".join(parts))
            except requests.Timeout:
                self.bus.say("gemini: timeout")
                continue
            except Exception as exc:
                self.bus.say(f"gemini: {type(exc).__name__}")
                continue

            if not data:
                continue

            lead = (data.get("lead") or "").strip()
            if not lead:
                continue

            self.bus.put_drop_oldest(
                self.bus.suggestions,
                Suggestion(
                    lead=lead,
                    points=[p.strip() for p in data.get("points", [])][:3],
                    kind=data.get("kind", "summary"),
                )
            )
