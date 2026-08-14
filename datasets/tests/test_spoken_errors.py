"""Failures must be sayable in one breath.

The regression this guards against is concrete: Nod once read a full
`ConnectionError` aloud -- host, port, retry count and URL -- which took about
fifteen seconds and told the user nothing they could act on. The first case
below is that exact string.

The assertions are deliberately mechanical rather than about wording: no URLs,
no ports, no exception class names, nothing long. Anything that passes those is
at worst bland, and bland is fine.

    python datasets\tests\test_spoken_errors.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests                                            # noqa: E402

from copilot import failure                                # noqa: E402
from copilot.llm_util import QuotaExhausted                # noqa: E402

MAX_SPOKEN = 80

# Real failures, including the one from the bug report.
CASES = [
    requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=60348): Max retries "
        "exceeded with url: /json/new?https://music.youtube.com/search?q=lofi "
        "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection "
        "object at 0x0000021F>: Failed to establish a new connection: "
        "[WinError 10061] No connection could be made'))"),
    QuotaExhausted("out of Gemini quota"),
    RuntimeError("HTTP 429: {'error': {'code': 429, 'status': "
                 "'RESOURCE_EXHAUSTED'}}"),
    RuntimeError("Chrome did not open its debugging port"),
    RuntimeError("Chrome exited immediately — most likely another Chrome is "
                 "already holding C:\\Users\\paulm\\.nod\\chrome"),
    TimeoutError("Read timed out. (read timeout=25)"),
    "pre-join screen never appeared",
    "could not confirm the microphone is off",
    "no playable result for 'lofi hip hop radio'",
    "Chrome profile is not signed in to Google",
    RuntimeError("no OAuth client at C:\\Users\\paulm\\.nod\\credentials.json"),
    requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=11434)"),
    ValueError("something nobody anticipated"),
]

BANNED = [
    (re.compile(r"https?://"), "a URL"),
    (re.compile(r"\bport\b|\b\d{4,5}\b"), "a port or long number"),
    (re.compile(r"[A-Za-z]+Error\b"), "an exception class name"),
    (re.compile(r"[A-Z]:\\\\|[A-Z]:\\"), "a filesystem path"),
    (re.compile(r"[{}\[\]<>]"), "brackets from a repr"),
]


def main() -> int:
    failures = 0

    print(f"spoken() — max {MAX_SPOKEN} chars, no machinery\n")
    for case in CASES:
        said = failure.spoken(case)
        problems = [why for pat, why in BANNED if pat.search(said)]
        if len(said) > MAX_SPOKEN:
            problems.append(f"too long ({len(said)})")
        if not said.strip():
            problems.append("empty")
        ok = not problems
        failures += 0 if ok else 1
        src = str(case)[:44].replace("\n", " ")
        print(f"  {'OK  ' if ok else 'FAIL'} {src!r:48} -> {said!r}")
        for why in problems:
            print(f"        contains {why}")

    # Right cause, not just a safe-looking sentence. A refused connection to
    # 11434 is Ollama, not Chrome, and saying "browser" sends you to the wrong
    # place entirely.
    print("\nspoken() — points at the right thing")
    routing = [
        (CASES[0], "browser"),                              # port 60348, CDP
        (CASES[-2], "local model"),                         # port 11434, Ollama
        (CASES[1], "quota"),
    ]
    for case, expect in routing:
        said = failure.spoken(case)
        ok = expect in said.lower()
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} expects {expect!r:14} -> {said!r}")

    # The card keeps the detail -- that is the whole point of splitting them.
    print("\ncard_detail() — keeps detail, one line")
    detail = failure.card_detail(CASES[0])
    checks = [
        ("fits one line", len(detail) <= 68),
        ("keeps something identifying", "HTTPConnectionPool" in detail),
        ("empty input degrades", failure.card_detail("") == "no detail"),
    ]
    for name, ok in checks:
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print(f"        {detail!r}")

    print(f"\nFAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
