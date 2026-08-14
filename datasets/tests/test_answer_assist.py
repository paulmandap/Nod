"""Meeting answer-assist: does it suggest answers, and does it invent them?

Two cases, and the second is the important one. With work context, the points
should be things the user could actually say, drawn from their own notes. With
*no* work context, the model must not fill the gap -- a fabricated status
spoken aloud in a meeting gets believed and acted on, which is worse than
saying nothing.

Needs GEMINI_API_KEY. Costs 2 requests.

    python datasets\tests\test_answer_assist.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.llm import MODEL, SCHEMA, SYSTEM                # noqa: E402
from copilot.llm_util import first_candidate_text, post_gemini   # noqa: E402

ENDPOINT = (f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{MODEL}:generateContent")

TRANSCRIPT = (
    "okay before we wrap up, Paul, can you give us a status update on the "
    "batch layer? we told the client it would be ready this sprint and I want "
    "to make sure that is still accurate before I send the note."
)

WORK_CONTEXT = """# What I'm working on
- (2026-08-03) batch layer ingestion is done and merged
- (2026-08-03) blocked on the vendor SOC2 audit before we can turn it on
- (2026-08-02) Priya owns the client comms for this
"""


def call(work: str) -> dict:
    parts = [f"<transcript>\n{TRANSCRIPT}\n</transcript>"]
    if work:
        parts.append(f"<my_work_context>\n{work}\n</my_work_context>")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": "\n\n".join(parts)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300,
                             "responseMimeType": "application/json",
                             "responseSchema": SCHEMA,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    payload = post_gemini(requests.Session(), ENDPOINT,
                          os.environ["GEMINI_API_KEY"], body)
    return json.loads(first_candidate_text(payload))


def show(label: str, d: dict) -> None:
    print(f"\n--- {label} ---")
    print(f"  kind : {d['kind']}")
    print(f"  lead : {d['lead']}")
    for p in d["points"]:
        print(f"     - {p}")


def main() -> int:
    failures = 0

    with_ctx = call(WORK_CONTEXT)
    show("WITH work context", with_ctx)
    blob = " ".join(with_ctx["points"]).lower()

    checks = [
        ("kind is reply", with_ctx["kind"] == "reply"),
        ("lead is the question, not a description",
         "?" in with_ctx["lead"] or "batch" in with_ctx["lead"].lower()),
        ("has at least 2 suggested answers", len(with_ctx["points"]) >= 2),
        ("answers use the work context (audit / merged / done / priya)",
         any(w in blob for w in ("audit", "soc2", "merged", "done", "priya",
                                 "blocked"))),
    ]

    without = call("")
    show("WITHOUT work context", without)
    blob2 = " ".join(without["points"]).lower()

    # The failure mode: asserting a specific state it cannot know.
    fabricated = any(w in blob2 for w in ("is done", "is complete", "finished",
                                          "on track", "ready this sprint",
                                          "merged"))
    checks.append(("no fabricated status when context is empty", not fabricated))

    print("\nCHECKS")
    for name, ok in checks:
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")

    print(f"\nFAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
