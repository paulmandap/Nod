"""Every test suite, one command, one exit code.

There was no runner before, only a list of eight commands in the RUNBOOK to
paste one at a time. That is not merely inconvenient -- it hid a real hole.
Two of the suites (`test_wake.py`, `test_filler.py`) print `N/M passed` while
the rest print `FAILURES: n`, so the obvious shortcut of grepping the output for
"FAILURES" reports those two as passing no matter what they actually did.

So this checks **exit codes**, which every suite has always set correctly, and
never parses output.

    python datasets\tests\run_tests.py            # everything that needs nothing
    python datasets\tests\run_tests.py --all      # including network + API key
    python datasets\tests\run_tests.py --frozen   # including the built exe
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# Suites that need nothing but the source tree.
OFFLINE = [
    "test_startup.py",
    "test_config.py",
    "test_wake.py",
    "test_filler.py",
    "test_endpointing.py",
    "test_awake_window.py",
    "test_barge_in.py",
    "test_spoken_errors.py",
    "test_meet_gate.py",
    "test_quota.py",
    "test_persona.py",
    "test_supervise.py",
]

# Needs GEMINI_API_KEY and the network, and spends real quota.
ONLINE = ["test_answer_assist.py"]

# Needs a completed PyInstaller build in dist/.
FROZEN = ["test_no_secrets.py", "test_frozen.py"]


def run(name: str) -> tuple[str, bool, float, str]:
    path = HERE / name
    if not path.exists():
        return name, False, 0.0, "missing"
    started = time.time()
    proc = subprocess.run([sys.executable, str(path)], cwd=str(ROOT),
                          capture_output=True, text=True)
    elapsed = time.time() - started
    if proc.returncode == 0:
        return name, True, elapsed, ""
    # The last non-empty line is where every suite puts its summary.
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    detail = tail[-1].strip() if tail else (proc.stderr or "").strip()[:90]
    return name, False, elapsed, detail


def main() -> int:
    ap = argparse.ArgumentParser(prog="run_tests")
    ap.add_argument("--all", action="store_true",
                    help="include suites that need the network and an API key")
    ap.add_argument("--frozen", action="store_true",
                    help="include suites that check the built exe in dist/")
    args = ap.parse_args()

    suites = list(OFFLINE)
    if args.all:
        if os.environ.get("GEMINI_API_KEY"):
            suites += ONLINE
        else:
            print("skipping online suites: GEMINI_API_KEY is not set\n")
    if args.frozen:
        suites += FROZEN

    failed = []
    print(f"Running {len(suites)} suites\n")
    for name in suites:
        name, ok, elapsed, detail = run(name)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<26} {elapsed:5.1f}s"
              f"{'  ' + detail if detail else ''}")
        if not ok:
            failed.append(name)

    print()
    if failed:
        print(f"{len(failed)} of {len(suites)} suites FAILED: "
              f"{', '.join(failed)}")
        print("Re-run one on its own to see why, e.g.:")
        print(f"    python datasets\\tests\\{failed[0]}")
        return 1
    print(f"All {len(suites)} suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
