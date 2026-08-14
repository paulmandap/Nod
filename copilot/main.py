"""Entrypoint.

Threading model, which is the part that usually goes wrong:

    audio-capture ──> bus.audio ──> transcriber ──> bus.transcripts ─┐
                                                                     ├─> router
    screen-reader ──────────────────────────────> bus.screen ────────┘   thread
                                                                          │
                                                                    ContextStore
                                                                          │
                                                        suggester ────────┘
                                                             │
                                                      bus.suggestions
                                                             │
                                             HUD (Qt main thread, QTimer poll)

Qt widgets may only be touched from the main thread, so the HUD never reads
from a worker directly — a QTimer drains the queue on the main thread instead.
Every worker is a daemon and watches bus.stop, so Ctrl+C exits cleanly.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

# Standard streams, before anything can try to write to one.
#
# A windowed PyInstaller build has no console, and on Windows that leaves
# sys.stdout and sys.stderr as None rather than as something harmless. Any
# print() then raises AttributeError: 'NoneType' object has no attribute
# 'write'. audio.list_devices() and hardware.py both print, and a library
# writing a warning to stderr from a worker thread would kill that thread.
#
# Runs above the imports because an import-time warning from any dependency
# would hit the same hole.
if sys.stdout is None or sys.stderr is None:
    import io

    class _Null(io.TextIOBase):
        def write(self, _s):        # noqa: D102 - matches TextIOBase
            return 0

        def flush(self):            # noqa: D102
            return None

    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()

# Settings, applied to the environment before any module that reads it loads.
#
# agent.MODEL, brain.MODEL and llm.MODEL each build an endpoint URL from
# os.environ at *import* time, so a config file read after those imports would
# be ignored. apply_env() does os.environ.setdefault for every key, which also
# means a real environment variable still wins -- see config.py for the
# precedence rules. It sets HF_HOME too, which has to happen before
# faster_whisper is imported anywhere.
from . import config                                    # noqa: E402

config.apply_env()

# The Windows COM apartment, claimed before anything else can take it.
#
# This runs above the imports on purpose, which is why it is not with them.
# `soundcard` calls CoInitializeEx(..., COINIT_MULTITHREADED) at import, and
# Nod imports it -- through hardware.py, audio.py and listen.py -- long before
# QApplication is constructed. Qt's Windows platform plugin then calls
# OleInitialize(), which requires a single-threaded apartment, gets
# RPC_E_CHANGED_MODE back, and logs this as the first line of the session:
#
#     QWindowsContext: OleInitialize() failed:
#     "COM error 0x80010106: Cannot change thread mode after it is set."
#
# It is a warning, not a failure: Qt carries on, and nothing here needs OLE --
# the HUD is frameless and click-through, with no drag-and-drop, no native
# dialogs and no OLE clipboard. The cost is entirely that it is alarming, and
# that it is printed before every real error, so every unrelated problem looks
# like it might be this one.
#
# Claiming STA first reverses who loses the race. soundcard handles the
# RPC_E_CHANGED_MODE it then gets back -- it marks the apartment as not its own
# and carries on, see _COMLibrary in soundcard/mediafoundation.py -- and Qt
# does not handle the reverse. Microphone capture and WASAPI loopback were both
# checked working under STA before this went in.
if sys.platform == "win32":
    import ctypes

    COINIT_APARTMENTTHREADED = 0x2
    ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

from PyQt6.QtWidgets import QApplication              # noqa: E402

from .agent import Agent                              # noqa: E402
from .audio import LoopbackCapture, list_devices
from .bus import Bus
from .context import ContextStore
from .listen import CommandListener, MicCapture
from .llm import Suggester
from .overlay import HUD
from .supervise import Supervisor
from .transcribe import Transcriber
from .voice import Speaker
from . import doctor, log

# vision is imported lazily, inside main(), because importing it pulls in cv2
# (113 MB), PIL and pytesseract. Screen reading is off by default in the
# packaged build, so for most runs that is a third of the memory and a large
# slice of the startup time spent loading something nothing will call.


def pin_language(value: str | None) -> str | None:
    """Turn a --language argument into what faster-whisper wants.

    `None` there means "detect", which is a useful setting but an unhelpful
    thing to have to spell as a default. The flags take a language code and
    accept the word `auto` for detection, so both states are reachable from the
    command line now that the default is a pinned `en`.
    """
    if value is None:
        return None
    return None if value.strip().lower() in ("auto", "detect") else value.strip()


def router(bus: Bus, ctx: ContextStore, suggester: Suggester) -> None:
    """Fan transcript + OCR events into the shared context window.

    The inner try is not decoration. This loop had no exception handling at all,
    so one malformed item killed the only consumer of two queues -- with no
    message anywhere, since it is a bare function rather than a named worker.
    The supervisor now restarts it, but surviving a single bad item without
    dying at all is better than being restarted.
    """
    while not bus.stop.is_set():
        moved = False
        try:
            while not bus.transcripts.empty():
                ctx.add_transcript(bus.transcripts.get_nowait())
                suggester.note_input()
                moved = True
            while not bus.screen.empty():
                ctx.add_screen(bus.screen.get_nowait())
                moved = True
        except Exception as exc:
            log.exception("router", exc)
            bus.say(f"router: skipped an item ({type(exc).__name__})")
        if not moved:
            bus.stop.wait(0.2)


def install_hotkeys(bus: Bus, hud: HUD, speaker=None) -> None:
    """Global hotkeys — the HUD is click-through, so this is its control surface."""
    try:
        from pynput import keyboard
    except ImportError:
        bus.say("hotkeys: pynput not installed")
        return

    keys = {
        # These emit rather than call: see the signal declarations on HUD.
        # bus.force_refresh is a threading.Event, so it is safe to set directly.
        "<ctrl>+<alt>+h": hud.toggle_visible.emit,
        "<ctrl>+<alt>+<space>": bus.force_refresh.set,
        "<ctrl>+<alt>+c": hud.request_click_through.emit,
        # The two halves, on/off without a restart. They are independent
        # rather than exclusive -- both on is the normal way to run in a
        # meeting -- so they get a key each instead of one that cycles.
        "<ctrl>+<alt>+m": bus.toggle_meeting,
        "<ctrl>+<alt>+a": bus.toggle_agent,
    }
    if speaker is not None:
        # Shut up now. Nothing else could stop a long answer mid-flow -- the
        # wake word works, but needing to talk over something to make it stop
        # talking is the wrong shape for "be quiet".
        keys["<ctrl>+<alt>+s"] = speaker.interrupt

    listener = keyboard.GlobalHotKeys(keys)
    listener.daemon = True
    listener.start()


def main() -> int:
    ap = argparse.ArgumentParser(prog="copilot")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--audio-device", default=None,
                    help="loopback device name (macOS: BlackHole 2ch)")
    # English-only by default, which is a reversal -- this used to default to
    # the multilingual `small`, on the reasoning that `.en` models are only
    # marginally better on English and are actively dangerous on anything else,
    # since they do not fail loudly but emit fluent English nonsense.
    #
    # That reasoning was sound and the evidence still went the other way. A
    # multilingual model does not fail loudly either: given English audio it
    # drifts into whichever language the acoustics suit. From perf.log, all of
    # these are English spoken into the mic and decoded by `base`:
    #
    #     "go to youtube.com"  ->  "Kau tu youtube.com"
    #     "go to Facebook"     ->  "Koto Facebook"
    #     "hey Nod"            ->  "ให้นั้น?"
    #
    # Pinning the language does not prevent it -- the wake decode was already
    # pinned to `en` when it produced the Thai. The model itself has to be the
    # English one. Pass --language auto (and --model small) to get detection
    # back for a genuinely multilingual meeting.
    # Every default below is None on purpose. The real defaults live in
    # config.DEFAULTS, and config.resolve() fills in anything the user did not
    # pass, from ~/.nod/config.json first and DEFAULTS second. Baking them into
    # add_argument here would make an explicit flag indistinguishable from an
    # unset one, and the saved settings could never win.
    ap.add_argument("--model", default=None, help="faster-whisper model")
    ap.add_argument("--language", default=None,
                    help="language code (en, tl, es...), or 'auto' to detect")
    ap.add_argument("--device", default=None, choices=["cpu", "cuda"])
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--monitor", type=int, default=None)
    ap.add_argument("--ocr-interval", type=float, default=None)
    ap.add_argument("--tesseract", default=None, help="path to tesseract binary")
    # Paired flags, so a saved setting can be overridden in both directions.
    # store_true with default=None gives the tri-state this needs: absent is
    # None, present is True, and the --vision / --summary forms force False.
    ap.add_argument("--no-vision", action="store_true", default=None)
    ap.add_argument("--vision", action="store_false", dest="no_vision",
                    help="read the screen even if settings say not to")
    ap.add_argument("--no-summary", action="store_true", default=None,
                    help="skip the meeting summariser and its API quota")
    ap.add_argument("--summary", action="store_false", dest="no_summary",
                    help="summarise meetings even if settings say not to")
    ap.add_argument("--ocr-active-window", action="store_true", default=None,
                    help="OCR only the focused window instead of the whole screen")
    ap.add_argument("--agent", action="store_true", default=None,
                    help="listen on the microphone for 'hey Nod'")
    ap.add_argument("--no-agent", action="store_false", dest="agent",
                    help="start with the microphone off")
    ap.add_argument("--mic-device", default=None, help="microphone name")
    # Same reversal, and it matters more here than on the meeting side: the
    # wake word is two syllables with no context to detect a language from, so
    # a multilingual model has nothing to anchor on and guesses. The commands
    # are the other half of it -- "kau tu youtube.com" reached the classifier
    # as a real instruction and was answered, which is worse than being
    # misheard, because nothing about it looks like a failure.
    #
    # The cost is Taglish: "pakiplay naman ng music" will decode worse on
    # base.en. Pass --wake-model base --wake-language auto to take that trade
    # in the other direction.
    ap.add_argument("--wake-model", default=None,
                    help="faster-whisper model for the wake word")
    ap.add_argument("--wake-language", default=None,
                    help="command language (en, tl), or 'auto' to detect")
    ap.add_argument("--camera", default=None,
                    help="preferred camera name fragment, e.g. REDRAGON")
    ap.add_argument("--local-intent", action="store_true", default=None,
                    help="classify commands on a local Ollama model (no API quota)")
    ap.add_argument("--voice", default=None,
                    help="SAPI voice name fragment: David (male), Zira (female)")
    ap.add_argument("--speech-rate", type=int, default=None,
                    help="SAPI rate, -10 (slow) to 10 (fast); 0 is normal")
    ap.add_argument("--voice-engine", default=None, choices=["piper", "sapi"],
                    help="piper is the local neural voice; sapi is Windows'")
    ap.add_argument("--voice-model", default=None,
                    help="path to a specific piper .onnx voice")
    # The wake word's energy thresholds. Exposed because the built-in values are
    # tuned to a close-talk headset: on a laptop's built-in array mic, speech
    # lands well under START_RMS and the wake word never fires at all, with no
    # error and nothing in the log. Setup calibrates these; these flags and the
    # doctor are how you fix it without the dialog.
    ap.add_argument("--start-rms", type=float, default=None,
                    help="mic energy to start an utterance (default 0.020)")
    ap.add_argument("--stop-rms", type=float, default=None,
                    help="mic energy to end one (default 0.007)")
    ap.add_argument("--allow-unconfirmed-camera", action="store_true", default=None,
                    help="join a meeting even if the camera can't be confirmed off")
    ap.add_argument("--doctor", action="store_true",
                    help="check this machine can run Nod, then exit")
    ap.add_argument("--setup", action="store_true",
                    help="open the settings window before starting")
    ap.add_argument("--json", action="store_true",
                    help="with --doctor, print the report as JSON")
    args = ap.parse_args()
    args = config.resolve(args)
    args.language = pin_language(args.language)
    args.wake_language = pin_language(args.wake_language)

    if args.list_devices:
        list_devices()
        return 0

    # Always on, unlike perf logging. In a windowed build there is no console,
    # so an unhandled exception would otherwise leave no trace at all.
    log.install()

    bus = Bus()
    ctx = ContextStore(window_seconds=120)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from . import onboarding

    # --doctor comes first, and deliberately never triggers setup. It is what
    # you are told to run when Nod will not start, so it has to work on a
    # machine that has nothing configured -- being met by a settings dialog you
    # cannot fill in is the opposite of a diagnostic.
    if args.doctor:
        checks = doctor.run(quick=False)
        doctor.write_report(checks)
        if args.json:
            print(doctor.as_json(checks))
        else:
            onboarding.DoctorDialog(checks, args).exec()
        return 1 if doctor.summary(checks)[0] else 0

    # First run, or asked for. Runs in this process and continues straight into
    # startup afterwards: making someone relaunch after setup is exactly where a
    # non-technical user gives up.
    if args.setup or not config.exists():
        onboarding.SetupDialog(args).exec()
        cfg = config.load()
        config.apply_env(cfg)
        args = config.resolve(args, cfg)

    # Every startup writes a report, pass or fail. When a tester says "it
    # doesn't work", this file plus nod.log is the whole diagnosis.
    startup_checks = doctor.run(quick=True)
    doctor.write_report(startup_checks)
    if any(c.state == doctor.FAIL for c in startup_checks):
        onboarding.DoctorDialog(startup_checks, args, allow_continue=True).exec()

    hud = HUD(bus)
    hud.show()

    # The flags now set the *initial* mode rather than deciding which threads
    # exist. Everything is constructed and started; the gated workers idle
    # behind bus.meeting_on / bus.agent_on until switched on. A Python Thread
    # cannot be restarted once run() returns, so building them lazily would
    # make "start listening to my meeting" mean reloading a Whisper model.
    if not args.no_summary:
        bus.meeting_on.set()
    if args.agent:
        bus.agent_on.set()

    suggester = Suggester(bus, ctx)
    # The speaker is passed into the listener rather than reached through the
    # bus because the listener must know, synchronously, whether Nod is
    # mid-sentence — otherwise it transcribes its own voice and wakes itself up.
    speaker = Speaker(bus, args.voice, args.speech_rate,
                      engine=args.voice_engine, voice_model=args.voice_model)

    # Factories rather than instances, because a Thread cannot be restarted once
    # run() has returned -- and restarting them is the entire point. Three of
    # these used to die permanently and silently on ordinary first-run problems
    # (no model download, no Tesseract, one bad queue item), leaving the app
    # looking fine and doing nothing.
    specs = {
        "router": lambda: threading.Thread(
            target=router, args=(bus, ctx, suggester),
            name="router", daemon=True),
        "audio-capture": lambda: LoopbackCapture(bus, args.audio_device),
        "transcriber": lambda: Transcriber(
            bus, args.model, args.device, args.compute_type, args.language),
        "suggester": lambda: suggester,
        "speaker": lambda: speaker,
        "mic-capture": lambda: MicCapture(bus, args.mic_device),
        "command-listener": lambda: CommandListener(
            bus, speaker, args.wake_model, args.device, args.compute_type,
            args.wake_language, start_rms=args.start_rms,
            stop_rms=args.stop_rms),
        "agent": lambda: Agent(
            bus, speaker, camera_hint=args.camera,
            local_intent=args.local_intent,
            allow_unconfirmed_camera=args.allow_unconfirmed_camera),
    }
    if not args.no_vision:
        from .vision import ScreenReader          # see the note by the imports

        specs["screen-reader"] = lambda: ScreenReader(
            bus, interval=args.ocr_interval, monitor=args.monitor,
            tesseract_cmd=args.tesseract or doctor.find_tesseract(),
            active_window=args.ocr_active_window)

    supervisor = Supervisor(
        bus, specs,
        # One attempt, not three, for the two that reload a Whisper model on
        # restart: seconds of CPU and hundreds of megabytes each time.
        limits={"transcriber": 1, "command-listener": 1},
        # Other objects hold direct references to these two, so a replacement
        # instance would not be the one anybody is talking to. Report, never
        # restart.
        no_restart=("speaker", "suggester"),
    )
    supervisor.start_all()
    supervisor.start()

    if args.hotkeys:
        install_hotkeys(bus, hud, speaker)
    else:
        bus.say("hotkeys: turned off in settings")

    def shutdown(*_):
        bus.stop.set()
        app.quit()

    signal.signal(signal.SIGINT, shutdown)
    app.aboutToQuit.connect(bus.stop.set)

    # Let Python handle SIGINT between Qt event loop iterations.
    from PyQt6.QtCore import QTimer
    idle = QTimer()
    idle.timeout.connect(lambda: None)
    idle.start(200)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
