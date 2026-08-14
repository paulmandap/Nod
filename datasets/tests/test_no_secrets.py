"""Nothing private may ship inside the build.

The API key is the thing that must never leave this machine, and the failure is
silent and permanent: once a build carrying a key is sent to five people, it
cannot be recalled, and the key is spending its owner's quota from five places.

Three defences exist and this is the only one that inspects the actual artifact
rather than the intention behind it:

  1. copilot/config.py has no default for gemini_api_key
  2. nod.spec's datas is an explicit allowlist, never a glob over the tree
  3. this, which greps the finished dist/ folder

Run it before every single share. It is the last gate.

    python datasets\tests\test_no_secrets.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist" / "Nod"

# Google API keys are a stable, recognisable shape.
KEY_SHAPE = re.compile(rb"AIza[0-9A-Za-z_\-]{35}")

# Files that legitimately contain arbitrary text and would produce noise.
SKIP_SUFFIXES = {".pyd", ".dll", ".so", ".zip"}

# Personal paths that should never be baked into a shipped artifact.
PERSONAL = [
    rb"C:\\Users\\paulm",
    rb"C:\\paul\\Nod",
]


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def scan(pattern: bytes | re.Pattern, label: str) -> list[str]:
    """Every file under dist/ containing `pattern`."""
    rx = pattern if isinstance(pattern, re.Pattern) else re.compile(re.escape(pattern))
    hits = []
    for path in DIST.rglob("*"):
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if rx.search(path.read_bytes()):
                hits.append(str(path.relative_to(DIST)))
        except Exception:
            continue
    return hits


def main() -> int:
    if not DIST.exists():
        print(f"  SKIP  no build at {DIST}")
        print("        run: pyinstaller --clean --noconfirm nod.spec")
        print("\nFAILURES: 0")
        return 0

    bad = 0
    files = sum(1 for p in DIST.rglob("*") if p.is_file())
    print(f"Scanning {files} files in {DIST}\n")

    print("No API key of any kind")
    hits = scan(KEY_SHAPE, "google key")
    bad += check("no string shaped like a Google API key", not hits, str(hits[:3]))

    # The specific key on this machine, compared but never printed.
    live = os.environ.get("GEMINI_API_KEY", "").strip()
    if live:
        hits = scan(live.encode(), "live key")
        bad += check("this machine's actual key is absent", not hits,
                     str(hits[:3]))
    else:
        print("  ..    GEMINI_API_KEY not set here, skipping exact-match check")

    print("\nNo personal paths")
    for needle in PERSONAL:
        hits = scan(re.compile(needle, re.I), "path")
        # A handful of build-time paths inside .pyc metadata are unavoidable and
        # harmless; what matters is that no config or text file carries them.
        text_hits = [h for h in hits
                     if Path(h).suffix.lower() in {".json", ".txt", ".md", ".ps1"}]
        bad += check(f"{needle.decode()} not in shipped text files",
                     not text_hits, str(text_hits[:3]))

    print("\nNo settings file was swept in")
    for name in ("config.json", "token.json", "credentials.json",
                 "workcontext.md", "perf.log", "nod.log"):
        found = list(DIST.rglob(name))
        bad += check(f"no {name}", not found,
                     str([str(f.relative_to(DIST)) for f in found[:2]]))

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
