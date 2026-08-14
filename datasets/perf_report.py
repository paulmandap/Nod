"""Turn perf.log into the numbers the latency targets are written against.

Markers are recorded per event; what matters is the gaps between them. This
pairs them into the stages a user actually waits through and reports p50/p90,
because a mean hides the occasional four-second reload that is the thing worth
finding.

    $env:NOD_PERF = "1"
    python -m copilot.main --agent --local-intent --no-summary
    python datasets\perf_report.py
"""

from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

LOG = Path(os.environ.get("NOD_HOME", Path.home() / ".nod")) / "perf.log"

# (label, from_tag, to_tag) -- each is a wait the user experiences.
STAGES = [
    ("utterance end -> decoded", "utterance.closed", "decode.done"),
    ("wake -> ack queued", "decode.done", "ack.queued"),
    ("ack queued -> speaking", "ack.queued", "speech.start"),
    ("WAKE -> AUDIBLE ACK", "utterance.closed", "speech.start"),
    ("command -> classified", "classify.start", "classify.done"),
    ("COMMAND -> DISPATCHED", "utterance.closed", "classify.done"),
    ("brain round trip", "brain.request", "brain.response"),
]


def load() -> list[tuple[float, str, str]]:
    if not LOG.exists():
        print(f"no log at {LOG} — run with NOD_PERF=1 first")
        sys.exit(1)
    rows = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        bits = line.split("\t")
        if len(bits) >= 2:
            rows.append((float(bits[0]), bits[1], bits[2] if len(bits) > 2 else ""))
    return rows


def pairs(rows, start_tag, end_tag) -> list[float]:
    """Each `start` matched to the next `end` after it, with no intervening
    second `start` -- so an abandoned utterance does not borrow the timing of
    the one after it."""
    out, open_at = [], None
    for ts, tag, _ in rows:
        if tag == start_tag:
            open_at = ts
        elif tag == end_tag and open_at is not None:
            out.append(ts - open_at)
            open_at = None
    return out


def main() -> None:
    rows = load()
    counts: dict[str, int] = {}
    for _, tag, _ in rows:
        counts[tag] = counts.get(tag, 0) + 1

    print(f"{LOG}  —  {len(rows)} markers\n")
    print("events:")
    for tag in sorted(counts):
        print(f"  {tag:22} {counts[tag]}")

    print("\nstages (ms):")
    print(f"  {'stage':28} {'n':>3} {'p50':>7} {'p90':>7} {'max':>7}")
    for label, a, b in STAGES:
        vals = sorted(pairs(rows, a, b))
        if not vals:
            print(f"  {label:28} {'-':>3} {'-':>7} {'-':>7} {'-':>7}")
            continue
        p90 = vals[min(int(len(vals) * 0.9), len(vals) - 1)]
        print(f"  {label:28} {len(vals):>3} "
              f"{statistics.median(vals)*1000:>7.0f} {p90*1000:>7.0f} "
              f"{max(vals)*1000:>7.0f}")

    dropped = counts.get("echo.dropped", 0)
    if dropped:
        print(f"\nself-hearing suppressed {dropped} time(s) — these are decodes "
              "not run, and wake-ups not fired")


if __name__ == "__main__":
    main()
