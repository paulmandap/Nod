"""Where bundled files live, which is not the same place twice.

Running from source, `speaker.ps1` sits next to `voice.py` and the command
dataset sits in `datasets/`. Inside a PyInstaller build there is no source tree:
everything declared in the spec's `datas` is unpacked under `sys._MEIPASS`, and
`Path(__file__)` points into a bundle path that does not contain them.

Two call sites depend on this and both fail quietly rather than loudly, which is
why they are worth being careful about:

    voice.py         speaker.ps1 missing -> Speaker cannot spawn -> Nod is mute
    local_intent.py  the dataset missing -> _shots() swallows the error and
                     returns [], dropping the local classifier from 98.4% to
                     87.3% accuracy with nothing said anywhere

The second is the reason `datasets/tests/test_startup.py` asserts the shot count
rather than trusting the runtime to complain.
"""

from __future__ import annotations

import sys
from pathlib import Path


def frozen() -> bool:
    """Are we running from a PyInstaller build?"""
    return getattr(sys, "frozen", False)


def root() -> Path:
    """The directory bundled data was unpacked to, or the repo root."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parents[1]


def resource(*parts: str) -> Path:
    """A path to a bundled file, relative to the project root."""
    return root().joinpath(*parts)
