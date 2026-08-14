"""Measure a local classifier against the Gemini one, on the same eval set.

The question this answers is narrow and worth stating plainly: *can a small
model running on this machine classify Nod's commands as well as the API does,
and how much faster?* Not "is it as smart" -- it will not be, and for open
questions it should not be asked to be. Only the classifier is on trial here.

Both accuracy and latency are reported because they trade against each other,
and because latency is the reason to consider this at all. An accurate
classifier that takes two seconds is what we already have.

    python datasets/benchmark.py --backend gemini
    python datasets/benchmark.py --backend ollama --model qwen2.5:3b-instruct
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.agent import SCHEMA, SYSTEM      # noqa: E402  the real prompt

EVAL = Path(__file__).parent / "commands.eval.jsonl"
TRAIN = Path(__file__).parent / "commands.train.jsonl"

INTENTS = ["attend_meeting", "next_meeting", "hardware", "hide", "ask",
           "play_music", "open_site", "note", "mode", "model", "style",
           "unknown"]


def load(path: Path = EVAL) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def few_shot(per_intent: int) -> list[dict]:
    """Worked examples as prior chat turns, drawn from the *train* split.

    From train and never eval: showing a model the answers it is about to be
    scored on measures nothing. Balanced per intent rather than sampled at
    random, because the point is to demonstrate the rare intents -- `style` is
    where the small model actually fails, and a random draw from 392 rows would
    include few of them.
    """
    if per_intent <= 0:
        return []
    by_intent: dict[str, list[dict]] = {}
    for row in load(TRAIN):
        by_intent.setdefault(row["intent"], []).append(row)

    turns: list[dict] = []
    for intent in INTENTS:
        for row in by_intent.get(intent, [])[:per_intent]:
            turns.append({"role": "user", "content": row["text"]})
            turns.append({"role": "assistant", "content": json.dumps(
                {"intent": row["intent"], "reply": "", "when": row["when"],
                 "style": row["style"], "query": row["query"]})})
    return turns


# --- backends -----------------------------------------------------------------

# The free tier caps requests per minute as well as per day, and a benchmark
# loop is the fastest possible way to hit the first one: firing 126 requests
# back to back returned 126 rejections that looked exactly like an exhausted
# daily quota, when a single call a moment later succeeded. Spacing them out is
# the difference between measuring the model and measuring the rate limiter.
_GEMINI_GAP = 4.5           # ~13/min, under the usual free-tier ceiling
_last_gemini = 0.0


def gemini(text: str, model: str) -> dict | None:
    global _last_gemini
    wait = _GEMINI_GAP - (time.time() - _last_gemini)
    if wait > 0:
        time.sleep(wait)
    _last_gemini = time.time()

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 200,
                             "responseMimeType": "application/json",
                             "responseSchema": SCHEMA},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"],
               "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=body, timeout=30)

    if r.status_code == 429:            # one backoff, then give up on this row
        time.sleep(20)
        _last_gemini = time.time()
        r = requests.post(url, headers=headers, json=body, timeout=30)

    if r.status_code != 200:
        return {"_error": f"HTTP {r.status_code}"}
    parts = r.json()["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in parts))


# Ollama is given the same instructions plus an explicit JSON shape. Local
# models follow a schema far less reliably than the API does, so format
# failures are counted separately from wrong answers -- they are a different
# problem with a different fix (grammar-constrained decoding, or fine-tuning).
OLLAMA_SUFFIX = """

Reply with ONLY a JSON object, no prose and no code fence:
{"intent": "<one of: %s>", "reply": "", "when": "", "style": "", "query": ""}""" % (
    ", ".join(INTENTS))


def ollama(text: str, model: str, shots: list[dict] | None = None) -> dict | None:
    messages = [{"role": "system", "content": SYSTEM + OLLAMA_SUFFIX}]
    messages += shots or []
    messages.append({"role": "user", "content": text})
    r = requests.post(
        "http://127.0.0.1:11434/api/chat",
        json={"model": model,
              "messages": messages,
              "stream": False,
              "format": "json",
              "options": {"temperature": 0.1, "num_predict": 120}},
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["message"]["content"].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"_malformed": content[:80]}


# --- runner -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["gemini", "ollama"], required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shots", type=int, default=0,
                    help="worked examples per intent, from the train split")
    args = ap.parse_args()

    model = args.model or ("gemini-3.5-flash-lite" if args.backend == "gemini"
                           else "qwen2.5:3b-instruct")
    rows = load()
    if args.limit:
        rows = rows[:args.limit]

    shots = few_shot(args.shots) if args.backend == "ollama" else []
    if shots:
        print(f"{len(shots)//2} worked examples in the prompt\n")

    def call(text: str, model: str):
        return (ollama(text, model, shots) if args.backend == "ollama"
                else gemini(text, model))

    hits = malformed = errors = 0
    latencies: list[float] = []
    confusion: dict[tuple[str, str], int] = {}

    print(f"{args.backend} / {model} — {len(rows)} examples\n")

    for i, row in enumerate(rows, 1):
        t = time.time()
        try:
            got = call(row["text"], model)
        except Exception as exc:
            errors += 1
            print(f"  [{i}] ERROR {type(exc).__name__}")
            continue
        latencies.append(time.time() - t)

        if got is None or "_error" in got:
            errors += 1
            continue
        if "_malformed" in got:
            malformed += 1
            continue

        want, have = row["intent"], got.get("intent", "")
        if want == have:
            hits += 1
        else:
            confusion[(want, have)] = confusion.get((want, have), 0) + 1

        if i % 25 == 0:
            print(f"  {i}/{len(rows)}  running accuracy "
                  f"{hits / max(i - errors - malformed, 1):.1%}")

    scored = len(latencies) - malformed
    print("\n" + "=" * 58)
    print(f"accuracy      {hits}/{max(scored,1)} = {hits/max(scored,1):.1%}")
    print(f"malformed     {malformed}")
    print(f"errors        {errors}")
    if latencies:
        print(f"latency  p50  {statistics.median(latencies)*1000:6.0f} ms")
        print(f"         p90  {sorted(latencies)[int(len(latencies)*0.9)-1]*1000:6.0f} ms")
        print(f"         mean {statistics.mean(latencies)*1000:6.0f} ms")
    if confusion:
        print("\nmisclassifications (expected -> predicted):")
        for (want, have), n in sorted(confusion.items(), key=lambda x: -x[1]):
            print(f"  {want:16} -> {have:16} {n}")


if __name__ == "__main__":
    main()
