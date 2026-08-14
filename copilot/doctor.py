"""Is this machine able to run Nod, and if not, what exactly is missing.

Written for the support conversation, not for the developer. Every failure
carries a `fix` line in the imperative, in words someone who has never opened
PowerShell can act on, and the report groups by consequence rather than by
subsystem -- "what does not work" is the question being asked, not "which
module".

Deliberately free of Qt so it can run headless in a test. `onboarding.py` wraps
the same data in a dialog for the packaged build, which has no console to print
to.

Everything here reuses what already exists -- `hardware.detect`,
`meet.browser_path`, `local_intent.available`, `calibrate.sample` -- rather than
re-implementing detection. A doctor that probes differently from the app is a
doctor that lies.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, paths

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    state: str                      # ok | warn | fail
    detail: str = ""                # what is true right now
    fix: str = ""                   # what to do about it, imperative
    blocks: str = ""                # which feature is dead without it


def _check(name: str, fn, blocks: str = "") -> Check:
    """Run one probe. An exception is a failed check, never a crashed doctor."""
    try:
        return fn()
    except Exception as exc:
        return Check(name, FAIL, f"{type(exc).__name__}: {exc}",
                     "This one could not be tested. Send this report on.",
                     blocks)


# --- individual checks -------------------------------------------------------

# Import name -> what stops working. Kept as import names rather than pip names
# because that is what actually has to resolve at runtime; the two differ for
# three of these, which is exactly how websocket-client went missing from
# requirements.txt without anyone noticing.
PACKAGES = {
    "numpy": "everything",
    "requests": "everything",
    "PyQt6": "the overlay",
    "soundcard": "the microphone",
    "faster_whisper": "hearing you",
    "pynput": "the keyboard shortcuts",
    "websocket": "joining meetings, music, opening sites",
    "googleapiclient": "Google Calendar",
    "google_auth_oauthlib": "Google Calendar",
    "dateutil": "recurring meetings",
}


def check_packages() -> Check:
    missing = [name for name in PACKAGES if importlib.util.find_spec(name) is None]
    if not missing:
        return Check("Python packages", OK, f"all {len(PACKAGES)} present")
    broken = ", ".join(sorted({PACKAGES[m] for m in missing}))
    fix = ("This copy of Nod is incomplete - reinstall it."
           if paths.frozen() else
           "Run:  pip install -r requirements.txt")
    return Check("Python packages", FAIL,
                 f"missing: {', '.join(missing)}", fix, broken)


def check_api_key() -> Check:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    where = config.source_of("gemini_api_key")
    if not key:
        return Check("Gemini API key", FAIL, "not set",
                     "Open Settings and paste a key from "
                     "aistudio.google.com/apikey - it is free.",
                     "answering questions, understanding commands")

    note = {"env": "set in Windows, not in Nod's settings",
            "file": "set in Nod's settings"}.get(where, "set")
    try:
        import requests

        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key}, timeout=6)
        if r.status_code == 200:
            return Check("Gemini API key", OK, f"working ({note})")
        if r.status_code in (400, 401, 403):
            return Check("Gemini API key", FAIL, f"rejected (HTTP {r.status_code})",
                         "The key is not valid. Open Settings and paste a new "
                         "one from aistudio.google.com/apikey.",
                         "answering questions, understanding commands")
        if r.status_code == 429:
            return Check("Gemini API key", WARN, "out of quota for now",
                         "Google's free daily limit is used up. It resets "
                         "tomorrow.")
        return Check("Gemini API key", WARN, f"HTTP {r.status_code} ({note})",
                     "Could not confirm the key works. Check again later.")
    except Exception:
        return Check("Gemini API key", WARN, f"{note}, but could not be checked",
                     "No internet, or something is blocking Google. The wake "
                     "word still works offline.")


def check_microphone() -> Check:
    from . import hardware

    hw = hardware.detect()
    if not hw.microphone:
        return Check("Microphone", FAIL, "none detected",
                     "Plug in a microphone or headset, then run this again.",
                     "everything - Nod cannot hear you")
    return Check("Microphone", OK, hw.microphone)


def check_mic_level(cfg: dict) -> Check:
    """Is the room already louder than the threshold that starts an utterance?

    This is the check that catches the most likely silent failure: a laptop
    array mic whose speech never reaches START_RMS, or a noisy room whose idle
    level sits above it.
    """
    from . import calibrate

    start = float(cfg.get("start_rms", config.DEFAULTS["start_rms"]))
    levels = calibrate.sample(1.5, cfg.get("mic_device"))
    noise = sorted(levels)[int(len(levels) * 0.9)] if levels else 0.0

    if noise > start:
        return Check("Microphone level", WARN,
                     f"room noise {noise:.4f} is above the {start:.4f} threshold",
                     "Your room is louder than Nod's trigger, so it may wake by "
                     "itself. Open Settings and press Calibrate.")
    if noise > start * 0.6:
        return Check("Microphone level", WARN,
                     f"room noise {noise:.4f}, threshold {start:.4f}",
                     "Close to triggering by itself. Calibrate if it misbehaves.")
    return Check("Microphone level", OK,
                 f"room is quiet ({noise:.4f}, threshold {start:.4f})")


def check_loopback() -> Check:
    import soundcard as sc

    try:
        name = sc.default_speaker().name
        sc.get_microphone(name, include_loopback=True)
        return Check("Speaker capture", OK, name)
    except Exception as exc:
        return Check("Speaker capture", WARN, str(exc)[:60],
                     "Nod cannot hear what your speakers play, so meeting "
                     "summaries will not work. Talking to Nod still will.",
                     "meeting summaries")


def check_camera() -> Check:
    from . import hardware

    cams = hardware.cameras()
    if not cams:
        # Not a failure: plenty of desktops have no webcam, and Nod joins
        # meetings with the camera off anyway.
        return Check("Camera", OK, "none detected (that is fine)")
    return Check("Camera", OK, cams[0])


def check_voice(cfg: dict | None = None) -> Check:
    """Which voice will Nod actually speak with?

    Piper first, because it is what the user hears when a model is present.
    Falling back to SAPI is fine and silent, so the report has to say which one
    is live -- "why does it sound different today" is otherwise unanswerable.
    """
    cfg = cfg or {}
    if (cfg.get("voice_engine") or "piper") != "sapi":
        # Resolved by asking a Speaker, not by globbing ~/.nod/voices. Those
        # two answers differ on a fresh install -- there is a voice bundled
        # inside the application that the folder knows nothing about -- and a
        # doctor that reports "Windows speech" while the app is using a neural
        # voice is worse than one that says nothing.
        from .bus import Bus
        from .voice import Speaker

        speaker = Speaker(Bus(), voice_model=cfg.get("voice_model"),
                          speaker_id=cfg.get("voice_speaker_id"))
        model = speaker._piper_model()
        if model:
            try:
                import piper  # noqa: F401

                detail = f"{model.stem} (local neural)"
                acc = cfg.get("accent")
                if acc and acc not in ("none", "neutral"):
                    detail += f", {acc} pronunciation"
                return Check("Voice", OK, detail)
            except Exception:
                return Check("Voice", WARN,
                             "a voice is available but piper is not installed",
                             "Reinstall Nod, or run: pip install piper-tts")
        # No model anywhere: SAPI is the intended fallback, not a failure.
    return _check_sapi_voice()


def _check_sapi_voice() -> Check:
    """Can PowerShell actually run the speech script?

    Catches AppLocker and Constrained Language Mode on managed laptops, which
    otherwise present as Nod silently never speaking.
    """
    import subprocess

    script = paths.resource("copilot", "speaker.ps1")
    if not script.exists():
        return Check("Voice", FAIL, "speaker.ps1 is missing",
                     "This copy of Nod is incomplete - reinstall it.",
                     "Nod speaking out loud")
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NoLogo", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-VoiceHint", "David", "-Rate", "0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = (proc.stdout.readline() or "").strip()
        try:
            proc.stdin.write("__NOD_EXIT__\n")
            proc.stdin.flush()
            proc.wait(timeout=3)
        except Exception:
            proc.terminate()
        if ready == "READY":
            return Check("Voice", OK, "Microsoft David (Windows speech)")
        return Check("Voice", FAIL, f"speaker.ps1 said {ready!r}",
                     "Windows would not start Nod's voice. This PC may block "
                     "PowerShell scripts.", "Nod speaking out loud")
    except Exception as exc:
        return Check("Voice", FAIL, str(exc)[:60],
                     "Windows would not start Nod's voice.",
                     "Nod speaking out loud")


def check_browser() -> Check:
    from . import meet

    try:
        return Check("Browser", OK, f"{meet.browser_name()} ({meet.browser_path()})")
    except Exception:
        return Check("Browser", WARN, "no Chrome, Brave or Edge found",
                     "Install Brave, Chrome or Edge if you want Nod to open "
                     "sites, play music or join meetings.",
                     "music, opening sites, joining meetings")


TESSERACT_GUESSES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    guesses = list(TESSERACT_GUESSES)
    if local:
        guesses.append(str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
    for guess in guesses:
        if Path(guess).exists():
            return guess
    return None


def check_tesseract() -> Check:
    found = find_tesseract()
    if found:
        return Check("Screen reading", OK, found)
    return Check("Screen reading", WARN, "Tesseract is not installed",
                 "Nod will not read your screen. That is fine unless you want "
                 "meeting summaries. Install it from "
                 "github.com/UB-Mannheim/tesseract/wiki if you do.",
                 "reading your screen")


def check_ollama() -> Check:
    from . import local_intent

    if local_intent.available(timeout=1.5):
        return Check("Local model", OK, f"{local_intent.MODEL} is running")
    return Check("Local model", WARN, "Ollama is not running",
                 "Only needed if you want Nod to understand commands without "
                 "using your Google quota. Safe to ignore.")


def check_model(cfg: dict) -> Check:
    from . import models

    name = cfg.get("wake_model", config.DEFAULTS["wake_model"])
    if models.present(name):
        return Check("Nod's ears", OK, f"{name} is downloaded")
    size = models.EXPECTED_MB.get(name, 150)
    return Check("Nod's ears", WARN, f"{name} is not downloaded yet",
                 f"The first time you speak to Nod it will download about "
                 f"{size} MB. This happens once. You can do it now from "
                 f"Settings.")


def check_disk() -> Check:
    total, used, free = shutil.disk_usage(str(config.NOD_HOME.anchor or "C:\\"))
    gb = free / 1e9
    if gb < 1:
        return Check("Disk space", FAIL, f"{gb:.1f} GB free",
                     "Free up space - Nod needs about 1 GB.", "everything")
    if gb < 3:
        return Check("Disk space", WARN, f"{gb:.1f} GB free",
                     "Getting tight. Nod's models need about 1 GB.")
    return Check("Disk space", OK, f"{gb:.0f} GB free")


def check_network() -> Check:
    import requests

    for name, url in (("Google", "https://generativelanguage.googleapis.com"),
                      ("model downloads", "https://huggingface.co")):
        try:
            requests.head(url, timeout=4)
        except Exception:
            return Check("Internet", WARN, f"cannot reach {name}",
                         "Nod needs internet to answer questions. The wake "
                         "word and 'stop' still work offline.")
    return Check("Internet", OK, "connected")


def check_calendar(cfg: dict) -> Check:
    from . import agenda

    if os.environ.get("NOD_ICS_URL", "").strip():
        return Check("Calendar", OK, "connected by calendar link")
    if agenda.TOKEN.exists():
        return Check("Calendar", OK, "connected to Google Calendar")
    if agenda.CREDENTIALS.exists():
        return Check("Calendar", WARN, "set up but not approved yet",
                     "Open Settings and click Connect calendar.",
                     "'attend my meeting'")
    return Check("Calendar", WARN, "not connected",
                 "Optional. Without it, 'attend my meeting' will not work.",
                 "'attend my meeting'")


def check_bundle() -> Check:
    """Did the packaging step drop a file the app needs at runtime?

    soundcard reads a .h file from its own package directory at import time
    (CFFI in ABI mode). If a build ever omits it, audio dies with an error that
    looks nothing like a missing data file. This is the canary.
    """
    import soundcard

    header = Path(soundcard.__file__).with_name("mediafoundation.py.h")
    missing = []
    if os.name == "nt" and not header.exists():
        missing.append("soundcard/mediafoundation.py.h")
    if not paths.resource("copilot", "speaker.ps1").exists():
        missing.append("speaker.ps1")
    if not paths.resource("datasets", "commands.train.jsonl").exists():
        missing.append("commands.train.jsonl")

    if missing:
        return Check("Nod's own files", FAIL, f"missing: {', '.join(missing)}",
                     "This copy of Nod is incomplete - download it again.",
                     "the microphone, or Nod's voice")
    return Check("Nod's own files", OK, "all present")


# --- running them ------------------------------------------------------------

def run(cfg: dict | None = None, quick: bool = False) -> list[Check]:
    """Every check. `quick` skips anything that touches the network or the mic.

    Startup uses quick=True so a slow network cannot delay the overlay; the
    Check-up shortcut uses the full set.
    """
    cfg = config.load() if cfg is None else cfg
    checks = [
        _check("Nod's own files", check_bundle),
        _check("Python packages", check_packages),
        _check("Microphone", check_microphone),
        _check("Voice", lambda: check_voice(cfg)),
        _check("Browser", check_browser),
        _check("Screen reading", check_tesseract),
        _check("Disk space", check_disk),
        _check("Nod's ears", lambda: check_model(cfg)),
        _check("Calendar", lambda: check_calendar(cfg)),
    ]
    if not quick:
        checks += [
            _check("Gemini API key", check_api_key),
            _check("Microphone level", lambda: check_mic_level(cfg)),
            _check("Speaker capture", check_loopback),
            _check("Camera", check_camera),
            _check("Local model", check_ollama),
            _check("Internet", check_network),
        ]
    else:
        # The key is the single most common problem, so it is checked even in
        # quick mode -- just without the network round trip.
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        checks.insert(1, Check("Gemini API key", OK, "set") if key else
                      Check("Gemini API key", FAIL, "not set",
                            "Open Settings and paste a key from "
                            "aistudio.google.com/apikey - it is free.",
                            "answering questions, understanding commands"))
    return checks


def summary(checks: list[Check]) -> tuple[int, int]:
    return (sum(1 for c in checks if c.state == FAIL),
            sum(1 for c in checks if c.state == WARN))


def as_text(checks: list[Check]) -> str:
    """The report, grouped by what it means rather than by subsystem."""
    groups = {
        "WHAT WORKS": [c for c in checks if c.state == OK],
        "WHAT DOES NOT": [c for c in checks if c.state == FAIL],
        "WORTH KNOWING": [c for c in checks if c.state == WARN],
    }
    lines = [f"Nod check-up{' ':>10}{time.strftime('%d %b %Y, %H:%M')}", ""]

    for title in ("WHAT DOES NOT", "WORTH KNOWING", "WHAT WORKS"):
        items = groups[title]
        if not items:
            continue
        lines.append(f"  {title}")
        for c in items:
            lines.append(f"    {c.name:<22}{c.detail}")
            if c.blocks and c.state == FAIL:
                lines.append(f"{'':<26}This stops: {c.blocks}.")
            if c.fix:
                for i, chunk in enumerate(_wrap(c.fix, 50)):
                    lines.append(f"{'':<26}{'-> ' if i == 0 else '   '}{chunk}")
        lines.append("")

    fails, warns = summary(checks)
    if not fails and not warns:
        lines.append("  Everything looks good.")
    else:
        lines.append(f"  {fails} problem{'s' if fails != 1 else ''}, "
                     f"{warns} thing{'s' if warns != 1 else ''} to know.")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def as_json(checks: list[Check]) -> str:
    import json

    return json.dumps([c.__dict__ for c in checks], indent=2)


def write_report(checks: list[Check]) -> Path | None:
    """Save the report where a tester can be asked to find it.

    Written as both text and JSON. The text is what a person copies into a
    message; the JSON exists because a windowed build has no stdout -- printing
    is not an option once console=False, so a file is the only way any
    automated check, including datasets/tests/test_frozen.py, can read the
    result out of the packaged exe.
    """
    try:
        config.NOD_HOME.mkdir(parents=True, exist_ok=True)
        out = config.NOD_HOME / "doctor.txt"
        out.write_text(as_text(checks), encoding="utf-8")
        (config.NOD_HOME / "doctor.json").write_text(as_json(checks),
                                                     encoding="utf-8")
        return out
    except Exception:
        return None
