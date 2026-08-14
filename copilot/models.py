"""Downloading Whisper models on purpose, instead of by accident.

Today the model is fetched lazily, the first time a worker thread needs it. That
is ~140 MB for `base.en` and ~460 MB for `small.en`, and `faster_whisper` passes
`tqdm_class=disabled_tqdm` internally, so there is no progress output of any
kind. From the outside: you say "hey Nod", nothing happens for two to five
minutes, and the only clue is a status line that stops being drawn as soon as a
card appears. Testers will report that as a hang, and they will be right to.

So the download moves to somewhere it can be *shown* -- the setup window and the
check-up dialog -- and the workers keep their existing error paths for the case
where someone skipped it.

Progress is measured by watching the cache directory grow rather than by hooking
into huggingface_hub. That is deliberate: `download_model` hard-codes its
progress class, so there is nothing to subscribe to, and reimplementing
`snapshot_download` with the right `allow_patterns` would break the next time
faster-whisper changes which files it wants. Polling a directory size is crude,
survives resumed downloads, and cannot get out of step with what is actually on
disk.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

# Rough on-disk sizes, for the progress bar only. Being wrong here shows a
# slightly off percentage; it never breaks a download.
EXPECTED_MB = {
    "tiny": 78, "tiny.en": 78,
    "base": 145, "base.en": 145,
    "small": 484, "small.en": 484,
    "medium": 1530, "medium.en": 1530,
    "large-v3": 3090,
}

REPO = "Systran/faster-whisper-{}"


def cache_root() -> Path:
    """Where HF_HOME points. config.apply_env sets this to ~/.nod/models."""
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_dir(name: str) -> Path:
    return cache_root() / f"models--Systran--faster-whisper-{name}"


def present(name: str) -> bool:
    """Is this model already downloaded and complete enough to load?

    Checks for the weights specifically rather than for the directory: an
    interrupted download leaves the folder and the metadata behind, and treating
    that as "downloaded" turns a resumable download into a load error.
    """
    root = model_dir(name)
    if not root.is_dir():
        return False
    return any(p.name == "model.bin" and p.stat().st_size > 1_000_000
               for p in root.rglob("model.bin"))


def size_mb(name: str) -> float:
    root = model_dir(name)
    if not root.is_dir():
        return 0.0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total / 1e6


def ensure(name: str, progress=None, cancel: threading.Event | None = None) -> bool:
    """Download `name` if it is not already here. True when it is ready.

    `progress(done_mb, total_mb)` is called about twice a second from a watcher
    thread. `cancel` stops the reporting, not the download itself -- huggingface
    offers no way to abort mid-transfer, and killing it would leave a partial
    file that `present()` would then have to reason about.
    """
    if present(name):
        return True

    total = EXPECTED_MB.get(name, 150)
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            if cancel is not None and cancel.is_set():
                return
            if progress:
                try:
                    progress(size_mb(name), total)
                except Exception:
                    return

    if progress:
        threading.Thread(target=watch, name="model-progress", daemon=True).start()

    try:
        from faster_whisper.utils import download_model

        download_model(name, cache_dir=str(cache_root()))
        return present(name)
    except Exception:
        return False
    finally:
        done.set()
        if progress:
            try:
                progress(size_mb(name), total)
            except Exception:
                pass
