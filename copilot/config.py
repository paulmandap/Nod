"""Settings that survive a restart, for people who do not use a terminal.

Nod was configured entirely by environment variables and command-line flags,
which is fine when the only user also wrote it. Handed to someone else it is
not: setting `GEMINI_API_KEY` on Windows means `setx` and a *new* terminal, and
the flags have to be retyped on every launch.

So there is now a `config.json` under `~/.nod`, and this module is the one place
that knows about it. Two design decisions are load-bearing:

  It exports to os.environ rather than being read directly. Nine modules already
  call `os.environ.get(...)`, and three of them -- agent.MODEL, brain.MODEL,
  llm.MODEL -- do it at *import* time to build an endpoint URL. Rewriting those
  to consult a config object would mean touching every one and getting the
  import order right forever after. `apply_env()` instead does
  os.environ.setdefault for each key, from main.py's pre-import block, and every
  existing call site keeps working untouched.

  setdefault, not assignment. A real environment variable therefore beats the
  file. That is deliberate: the machine this was written on has GEMINI_API_KEY
  set permanently via setx, and a config file that silently overrode it would
  mean debugging two sources of truth. `source_of()` exists so the doctor can
  say which one is in play, because "I pasted a key and nothing changed" is
  otherwise a genuinely confusing five minutes.

Precedence, highest first:

    explicit command-line flag  >  environment variable  >  config.json  >  default

Nothing here imports anything outside the standard library, so it is safe to
call before Qt, before soundcard, and before the COM apartment is claimed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Not imported from the other modules on purpose -- they each read NOD_HOME
# themselves, and this file has to work before any of them are importable.
NOD_HOME = Path(os.environ.get("NOD_HOME", Path.home() / ".nod"))
PATH = NOD_HOME / "config.json"

# config key -> environment variable the rest of the codebase already reads.
ENV_KEYS = {
    "gemini_api_key": "GEMINI_API_KEY",
    "gemini_model": "GEMINI_MODEL",
    "gemini_ask_model": "GEMINI_ASK_MODEL",
    "calendar_ics_url": "NOD_ICS_URL",
    "browser_path": "NOD_BROWSER",
    "brave_search_key": "NOD_BRAVE_KEY",
}

# Settings that are command-line flags rather than environment variables, with
# the value used when neither the flag nor the file supplies one.
#
# These differ from the old argparse defaults in three places, and all three are
# about the packaged build being handed to someone who has installed nothing:
#
#   model        small.en -> base.en   325 MB less to download before it works
#   no_summary   off -> on             meeting mode costs API quota and a model
#   no_vision    off -> on             screen reading needs Tesseract installed
#
# Running from source with explicit flags is unaffected; every documented
# command in the RUNBOOK passes what it wants.
DEFAULTS = {
    "model": "base.en",
    "language": "en",
    "wake_model": "base.en",
    "wake_language": "en",
    "device": "cpu",
    "compute_type": "int8",
    "monitor": 1,
    "ocr_interval": 1.0,
    "tesseract": None,
    "audio_device": None,
    "mic_device": None,
    "camera": None,
    "voice": None,
    "speech_rate": None,
    "voice_engine": None,     # None = piper if downloaded, else SAPI
    "voice_model": None,      # a specific .onnx; None = newest in ~/.nod/voices
    "voice_speaker_id": None, # which voice inside a multi-speaker model
    "voice_length_scale": None,  # >1.0 is slower and more composed

    "agent": True,
    "no_summary": True,
    "no_vision": True,
    "ocr_active_window": False,
    "local_intent": False,
    "start_rms": 0.020,
    "stop_rms": 0.007,
    "hotkeys": True,
    "allow_unconfirmed_camera": False,
}

# Never given a default, never written by anything but the user, and asserted
# absent from the built artifact by datasets/tests/test_no_secrets.py.
SECRET_KEYS = ("gemini_api_key", "brave_search_key", "calendar_ics_url")


def load() -> dict:
    """The saved settings, or {} if there are none or the file is unreadable.

    Never raises. A corrupt config must not stop the app starting -- the doctor
    reports it and the setup dialog rewrites it, which is a far better outcome
    than a JSONDecodeError traceback in a window that does not exist.
    """
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data: dict) -> bool:
    """Write settings atomically. True on success.

    Atomic because the alternative is a half-written config.json when the disk
    fills or the app is killed mid-save, and that reads as "Nod forgot my API
    key" -- the single most annoying thing this file could do.
    """
    try:
        NOD_HOME.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, PATH)
        return True
    except Exception:
        return False


def exists() -> bool:
    return PATH.is_file()


def apply_env(cfg: dict | None = None) -> None:
    """Publish settings into os.environ for the modules that read it.

    setdefault, so anything already set in the real environment wins. Call this
    before importing anything that reads os.environ at module scope.
    """
    cfg = load() if cfg is None else cfg

    for key, env in ENV_KEYS.items():
        value = cfg.get(key)
        if value:
            os.environ.setdefault(env, str(value))

    # Keep the Whisper models under ~/.nod rather than in the user's global
    # HuggingFace cache. One directory to check in the doctor, one to report the
    # size of, and "delete the .nod folder and start again" becomes true advice
    # instead of leaving a 500 MB orphan behind. Must precede any faster_whisper
    # or huggingface_hub import, which is why apply_env() runs where it does.
    #
    # Unless models are already in the default cache, in which case leave well
    # alone. Moving the goalposts on a machine that has already downloaded them
    # does not tidy anything up -- it hides 1.2 GB and silently re-downloads the
    # lot on the next launch. New installs get the tidy layout; existing ones
    # keep working.
    if not _legacy_cache_has_models():
        os.environ.setdefault("HF_HOME", str(NOD_HOME / "models"))


def _legacy_cache_has_models() -> bool:
    """Does the default HuggingFace cache already hold a Whisper model?"""
    if os.environ.get("HF_HOME"):
        return True             # somebody has already chosen; do not second-guess
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    try:
        return any(hub.glob("models--Systran--faster-whisper-*"))
    except Exception:
        return False


def source_of(key: str) -> str:
    """Where `key`'s value is coming from: "env", "file", or "default".

    Exists for one specific support conversation: someone pastes an API key into
    the setup window, nothing changes, because a stale environment variable is
    winning. The doctor prints this next to the key so that is visible rather
    than mysterious.
    """
    env = ENV_KEYS.get(key)
    if env and os.environ.get(env):
        # setdefault means the file may have been what set it. Distinguish by
        # asking whether the file has a value that differs.
        saved = load().get(key)
        if saved and str(saved) == os.environ.get(env):
            return "file"
        return "env"
    if key in load():
        return "file"
    return "default"


def resolve(args, cfg: dict | None = None):
    """Fill in every argparse value left as None, from the file then DEFAULTS.

    Pure apart from reading the file, so datasets/tests/test_config.py can check
    the precedence table without touching argparse or the filesystem.
    """
    cfg = load() if cfg is None else cfg
    for dest, default in DEFAULTS.items():
        if getattr(args, dest, None) is None:
            setattr(args, dest, cfg.get(dest, default))
    return args


def redacted(cfg: dict | None = None) -> dict:
    """The settings with secrets masked, safe to print into a support report."""
    cfg = load() if cfg is None else cfg
    out = dict(cfg)
    for key in SECRET_KEYS:
        if out.get(key):
            out[key] = f"set ({len(str(out[key]))} characters)"
    return out
