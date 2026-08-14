"""Answering an open question by looking things up first.

This is the tool-calling loop, and it is worth being clear about what it is not.
It is not RAG: RAG retrieves from a corpus you own, and nothing in your own
documents contains today's exchange rate. It is not MCP either -- MCP is a
protocol for *serving* tools to a model, which is useful when the tools live in
someone else's process, and here they live in tools.py, ten lines away.

What it is: the model is told which functions exist, it decides which to call,
Nod runs them and hands back the results, and the model answers from those
results. Three or four turns of a normal chat completion.

The loop is capped at MAX_ROUNDS. An uncapped tool loop is how you get a model
that searches, decides the snippet was unsatisfying, searches a near-identical
phrase, and does that until the quota is gone -- which on a free key is a real
cost, not a theoretical one.

Answers are written for a voice. Nod says them out loud, so the model is told
to give one or two sentences with the actual number or name in the first
clause, and no URLs -- a spoken citation is noise, and the HUD card carries the
source instead.
"""

from __future__ import annotations

import json
import os

import requests

from . import perf, tools
from .llm_util import post_gemini

# Deliberately a *different* model from the summariser's. The free-tier day
# quota is GenerateRequestsPerDayPerProjectPerModel -- 500 per model, not per
# key -- so putting the assistant on its own model gives it its own 500 and
# stops an afternoon of meeting summaries from leaving it unable to answer.
# gemini-3.5-flash-lite is also a better explainer, which is what "say it
# simply" actually needs.
MODEL = os.environ.get("GEMINI_ASK_MODEL", "gemini-3.5-flash-lite")
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

MAX_ROUNDS = 4

BASE_SYSTEM = """You are Nod, answering a question the user asked out loud.

You have tools that look things up. Use them. You are frequently wrong about \
anything time-sensitive -- prices, rates, scores, who currently holds an \
office, what version something is on, what happened recently -- and you are \
wrong in a way that sounds exactly like being right, so a confident guess is \
the worst thing you can produce. If the answer could have changed since you \
were trained, look it up before answering.

Do not describe your process. Never say "let me search" or "according to my \
search" -- just answer with what you found.

Your answer is read aloud, so: plain spoken language, no markdown, no bullet \
points, no URLs, no emoji, no numbered lists read out as "one dot". Put the \
actual number, name or date in the first clause. Round sensibly for speech -- \
"about 61 pesos to the dollar" is better than "61.254828".

If the tools found nothing useful, say you could not find it. Do not fall back \
on what you think you remember.

You are in a continuing conversation. Earlier turns are given to you. When the \
user says "explain that differently" or "what about the second one", they mean \
the thing you were just talking about -- do not ask them to repeat it."""

# Style is a persistent setting rather than something inferred per question,
# because "explain it like I'm five" is said once and meant until countermanded.
# Inferring it from each question separately loses it on the very next turn,
# which is the thing that makes an assistant feel like it is not listening.
STYLES = {
    "plain": """
Explain in everyday words. Assume no background knowledge and no jargon; if a \
technical term is unavoidable, say what it means in the same breath. Short \
sentences. Two to four of them for anything that needs explaining, one for a \
simple fact.""",

    "kid": """
Explain it the way you would to a curious ten-year-old. Very short sentences. \
Only common words. Use a comparison to something ordinary and physical when it \
helps -- kitchen things, toys, animals, weather. Never talk down, never say \
"it's simple" or "just", and do not add a moral at the end. Warm and matter of \
fact. Four to six short sentences is plenty.""",

    "technical": """
The user wants precision over accessibility. Use the correct terms without \
explaining them, include figures and units, and do not simplify away the \
caveats.""",
}

STEPS_NOTE = """

If the answer is a procedure -- how to cook, install, fix or do something -- \
finish with a line that begins exactly `STEPS:` followed by the steps separated \
by ` | `. Five words per step at most, imperative mood, no numbering. This line \
is stripped out before anything is spoken; it is for the overlay card. Example:

STEPS: Heat pan on medium | Add butter | Crack egg in | Cook until white sets"""


def system_prompt(style: str = "plain") -> str:
    return BASE_SYSTEM + "\n" + STYLES.get(style, STYLES["plain"]) + STEPS_NOTE


# One connection pool for the answering path. A question that triggers two tool
# rounds makes four requests, and a fresh TLS handshake each time was costing
# more than the model.
_session = requests.Session()


def split_steps(answer: str) -> tuple[str, list[str]]:
    """Pull the STEPS: line out of the answer.

    Returned separately because the two have different jobs: the prose is
    spoken, the steps are read off the overlay afterwards. Reading a list of
    steps aloud and then showing the same list is just saying everything twice.
    """
    steps: list[str] = []
    kept: list[str] = []
    for line in answer.splitlines():
        if line.strip().upper().startswith("STEPS:"):
            body = line.split(":", 1)[1]
            steps = [s.strip(" -•") for s in body.split("|") if s.strip()]
        else:
            kept.append(line)
    return " ".join(" ".join(kept).split()), steps


def ask(question: str, api_key: str | None = None, style: str = "plain",
        history: list[dict] | None = None) -> tuple[str, list[str], list[str]]:
    """Answer `question`, looking things up as needed.

    Returns (spoken_answer, sources, steps).
    """
    # The key travels as an argument rather than through os.environ. Writing to
    # the process environment from a worker thread to pass one string to a
    # function two frames down is a race waiting to happen, and it made the key
    # readable to every other module by accident.
    key = api_key or os.environ.get("GEMINI_API_KEY", "")

    # Prior turns first, so a follow-up like "explain that to a kid" has
    # something to refer back to.
    contents: list[dict] = list(history or [])
    contents.append({"role": "user", "parts": [{"text": question}]})
    sources: list[str] = []

    for round_no in range(MAX_ROUNDS):
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt(style)}]},
            "contents": contents,
            "tools": [{"functionDeclarations": tools.DECLARATIONS}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 400,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        perf.mark("brain.request", f"round={round_no}")
        payload = post_gemini(_session, ENDPOINT, key, body)
        perf.mark("brain.response", f"round={round_no}")

        cands = payload.get("candidates") or []
        if not cands:
            return "I couldn't work that out.", sources, []

        parts = cands[0].get("content", {}).get("parts") or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not calls:
            text = "".join(p.get("text", "") for p in parts).strip()
            spoken, steps = split_steps(text)
            return (spoken or "I couldn't find that."), sources, steps

        # Keep the model's own turn in the history, or the follow-up response
        # has nothing to attach to and Gemini rejects the conversation.
        contents.append({"role": "model", "parts": parts})

        responses = []
        for call in calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            result = tools.run(name, args)

            for hit in (result.get("results") or [])[:3]:
                title = hit.get("title", "")
                if title and title not in sources:
                    sources.append(title)
            if name == "exchange_rate" and result.get("as_of"):
                sources.append(f"er-api · {result['as_of']}")

            responses.append({"functionResponse": {"name": name,
                                                   "response": result}})

        contents.append({"role": "user", "parts": responses})

    return "That took too many steps, so I stopped.", sources, []
