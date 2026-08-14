"""Does the built exe actually work — checked by a machine, not a human.

This is the highest-value test in the suite, because freezing an application is
where things go missing *quietly*. PyInstaller's analyser follows imports, and
every one of these is invisible to it:

    websocket                 imported inside meet.Tab.__init__
    pynput.keyboard._win32    selected at runtime by platform
    soundcard's .h files      read from disk by CFFI at import
    speaker.ps1               opened by path, not imported
    commands.train.jsonl      opened by path, and its absence is swallowed

Every one fails only when a real user tries the feature, on their machine, with
no console to print to. So the exe is asked to examine itself: `--doctor` runs
the same checks the app runs at startup and writes them to ~/.nod/doctor.json,
which this reads back.

Requires a completed build. Skips cleanly without one, so it can sit in the
normal suite.

    pyinstaller --clean --noconfirm nod.spec
    python datasets\tests\test_frozen.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "dist" / "Nod" / "Nod.exe"
REPORT = Path(os.environ.get("NOD_HOME", Path.home() / ".nod")) / "doctor.json"

# Files the spec must have carried across. Named individually because each has
# its own silent failure mode.
BUNDLED = [
    ("copilot/speaker.ps1", "Nod would be mute"),
    ("datasets/commands.train.jsonl", "local intent drops to 87% accuracy"),
    ("soundcard/mediafoundation.py.h", "audio would not start"),
    ("googleapiclient/discovery_cache/documents/calendar.v3.json",
     "the calendar client would not build"),
]


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def main() -> int:
    if not EXE.exists():
        print(f"  SKIP  no build at {EXE}")
        print("        run: pyinstaller --clean --noconfirm nod.spec")
        print("\nFAILURES: 0")
        return 0

    bad = 0
    internal = EXE.parent / "_internal"

    print("Files the analyser could not have found on its own")
    for rel, consequence in BUNDLED:
        bad += check(f"{rel}", (internal / rel).exists(), f"({consequence})")

    # Note deliberately not checked here: whether module *folders* exist under
    # _internal. Pure-Python packages are compiled into the PYZ archive rather
    # than extracted, so websocket and google_auth_oauthlib are genuinely
    # present and genuinely invisible to a directory listing. The authoritative
    # check is the "Python packages" one below, which runs importlib inside the
    # frozen process -- strictly better evidence than a filename.

    print("\nThings the spec deliberately excluded")
    for name in ("onnxruntime", "sympy"):
        bad += check(f"{name} is absent", not (internal / name).exists(),
                     "(the ~97 MB exclusion still holds)")

    print("\nThe exe runs and can examine itself")
    if REPORT.exists():
        REPORT.unlink()
    started = time.time()
    proc = subprocess.run([str(EXE), "--doctor", "--json"],
                          capture_output=True, timeout=180)
    elapsed = time.time() - started
    bad += check("exits without crashing", proc.returncode in (0, 1),
                 f"exit {proc.returncode} in {elapsed:.0f}s")
    bad += check("wrote its report", REPORT.exists(), str(REPORT))

    if REPORT.exists():
        checks = json.loads(REPORT.read_text(encoding="utf-8"))
        bad += check("report parses", isinstance(checks, list) and checks,
                     f"{len(checks)} checks")

        by_name = {c["name"]: c for c in checks}

        # The two that prove the freeze itself worked. "Python packages" is the
        # one that matters most: it runs importlib.util.find_spec inside the
        # frozen process for every import Nod needs, including the four the
        # analyser cannot see, so a hidden import dropped from the spec fails
        # here rather than on a tester's machine.
        for name in ("Nod's own files", "Python packages"):
            got = by_name.get(name, {})
            bad += check(f"{name!r} passed inside the exe",
                         got.get("state") == "ok",
                         f"{got.get('state')}: {got.get('detail', '')[:60]}")

        packages = by_name.get("Python packages", {})
        bad += check("...and that covered every import, not a subset",
                     "all 10 present" in packages.get("detail", ""),
                     packages.get("detail", ""))

        # Anything else failing is a machine problem, not a build problem, so
        # it is reported rather than failed on.
        others = [c for c in checks
                  if c["state"] == "fail"
                  and c["name"] not in ("Nod's own files", "Python packages")]
        if others:
            print("\n  (machine-specific, not build problems:)")
            for c in others:
                print(f"     - {c['name']}: {c['detail'][:60]}")

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
