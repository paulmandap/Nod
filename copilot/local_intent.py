"""Intent classification on a local model, as an alternative to the API.

Measured on `datasets/commands.eval.jsonl`, 126 examples, qwen2.5:3b-instruct
on an RTX 3050:

    zero-shot   87.3%   581 ms p50
    16 examples 98.4%   606 ms p50

The gap between those two lines is the useful finding. Zero-shot, nine of
sixteen errors were the `style` intent -- "explain like I'm five" landing as
hide, unknown or ask. Sixteen worked examples in the prompt removed every one
of them for about 25 ms.

That is worth stating plainly because the obvious next step, fine-tuning, would
have cost hours and produced a model that is harder to change: adding a ninth
intent later means retraining, whereas here it means adding two lines to the
dataset. Fine-tuning is the answer when prompting has been tried and is not
enough. It had not been tried.

What this does not do is replace `brain.py`. Classification is a narrow,
structured task with eight outcomes, which is why a 3B model can do it. Open
questions need breadth this model does not have, and no local model of any size
knows today's exchange rate -- that is what `tools.py` is for.

Requires Ollama running with the model pulled:

    ollama pull qwen2.5:3b-instruct
"""

from __future__ import annotations

import json

import requests

from . import paths

ENDPOINT = "http://127.0.0.1:11434/api/chat"
_session = requests.Session()
MODEL = "qwen2.5:3b-instruct"
# Via paths.resource for the frozen build. _shots() swallows every exception, so
# if this path is ever wrong the only symptom is the classifier quietly dropping
# from 98.4% to 87.3%; test_startup.py asserts the shot count for that reason.
TRAIN = paths.resource("datasets", "commands.train.jsonl")

INTENTS = ["attend_meeting", "next_meeting", "hardware", "hide", "ask",
           "play_music", "open_site", "note", "mode", "model", "style",
           "unknown"]

SHOTS_PER_INTENT = 2

# Local models hold a schema far less reliably than the API, which validates
# against one server-side. Ollama's format=json plus an explicit shape gave 0
# malformed replies in 252 calls, so this is belt and braces that earns its keep.
SUFFIX = """

Reply with ONLY a JSON object, no prose and no code fence:
{"intent": "<one of: %s>", "reply": "", "when": "", "style": "", "query": ""}""" % (
    ", ".join(INTENTS))


def _shots() -> list[dict]:
    """Worked examples, balanced across intents, from the train split.

    Balanced rather than sampled: the intents that need demonstrating are the
    rare, abstract ones, and a random draw from 392 rows would under-represent
    exactly those.
    """
    try:
        rows = [json.loads(l) for l in TRAIN.read_text(encoding="utf-8").splitlines()
                if l.strip()]
    except Exception:
        return []

    by_intent: dict[str, list[dict]] = {}
    for row in rows:
        by_intent.setdefault(row["intent"], []).append(row)

    turns: list[dict] = []
    for intent in INTENTS:
        for row in by_intent.get(intent, [])[:SHOTS_PER_INTENT]:
            turns.append({"role": "user", "content": row["text"]})
            turns.append({"role": "assistant", "content": json.dumps(
                {"intent": row["intent"], "reply": "", "when": row["when"],
                 "style": row["style"], "query": row["query"]})})
    return turns


_CACHED: list[dict] | None = None


def shots() -> list[dict]:
    global _CACHED
    if _CACHED is None:
        _CACHED = _shots()
    return _CACHED


def available(timeout: float = 2.0) -> bool:
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=timeout)
        return any(m.get("name", "").startswith(MODEL.split(":")[0])
                   for m in r.json().get("models", []))
    except Exception:
        return False


# Used only when Gemini is unavailable. The instructions are mostly a list of
# things not to do, because the failure mode of a small model answering from
# memory is exactly the one tools.py exists to prevent: it will state an
# exchange rate, a score or a release date with total confidence and be two
# years out of date. Mute would be better than that. Honest and limited is
# better than either.
ANSWER_SYSTEM = """You are Nod, answering out loud because the usual model is \
unavailable. You are a small local model with no internet access and no tools.

Answer only what you can answer from general knowledge that does not change: \
definitions, how things work, arithmetic, spelling, stable facts of geography \
and history.

Refuse anything time-sensitive. Prices, exchange rates, scores, weather, news, \
who currently holds a position, what version something is on, what is open or \
happening now -- for all of these say you cannot check right now, and stop. Do \
not estimate, do not say "around", do not give a figure "as of" a date. A \
wrong number said confidently is worse than no answer, because it gets \
believed.

Two sentences at most. Plain spoken language, no markdown, no lists, no URLs."""


def answer(question: str, model: str = MODEL) -> str:
    """A best-effort local answer. Raises on transport failure."""
    r = _session.post(
        ENDPOINT,
        json={"model": model,
              "messages": [{"role": "system", "content": ANSWER_SYSTEM},
                           {"role": "user", "content": question}],
              "stream": False, "keep_alive": -1,
              "options": {"temperature": 0.2, "num_predict": 160}},
        timeout=90,
    )
    r.raise_for_status()
    return " ".join(r.json()["message"]["content"].split()).strip()


def classify(text: str, system: str, model: str = MODEL) -> dict | None:
    """Same contract as agent.Agent._classify: a dict, or None."""
    messages = [{"role": "system", "content": system + SUFFIX}]
    messages += shots()
    messages.append({"role": "user", "content": text})

    r = _session.post(
        ENDPOINT,
        json={"model": model, "messages": messages, "stream": False,
              "format": "json",
              # Ollama evicts an idle model after five minutes by default, so
              # the first command after a quiet spell paid a multi-second
              # reload -- which read as "Nod is randomly slow" rather than as
              # anything to do with idling. -1 keeps it resident. It costs
              # ~2.2 GB of VRAM continuously, which this card has spare.
              "keep_alive": -1,
              "options": {"temperature": 0.1, "num_predict": 120}},
        timeout=60,
    )
    r.raise_for_status()
    raw = r.json()["message"]["content"].strip()
    try:
        got = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # The local model is not schema-validated server-side, so an unknown intent
    # is possible in a way it is not with the API. Fall back rather than let a
    # made-up name reach the dispatch table.
    if got.get("intent") not in INTENTS:
        got["intent"] = "unknown"
    for field in ("reply", "when", "style", "query"):
        got.setdefault(field, "")
    return got
