"""Keeping the worker threads alive, and saying so when one is not.

The bug this exists for has bitten three times in different files. Every worker
is a daemon thread whose `run()` ends with a bare `return` on failure:

    transcribe.py   WhisperModel(...) raises -> bus.say(...) -> return
    vision.py       Tesseract missing        -> bus.say(...) -> return
    main.router     no try/except at all     -> dies with no message whatsoever

A Python thread cannot be restarted once `run()` returns, so each of those is
permanent for the session. Worse, they are *quiet*: the one status line goes to
the HUD's idle display, which stops being drawn the moment any suggestion card
appears. The overlay still says MEETING, audio still flows into a queue nobody
is reading, and nothing about the app looks broken. On the author's own machine
that is annoying. On a tester's machine it is the entire bug report: "it just
stopped doing anything".

Two pieces:

  guard()      wraps a worker's run() so an exception is logged and announced
               rather than vanishing. It works by assigning an *instance*
               attribute, which shadows the bound method -- so no worker file
               needs to change, and workers written later are covered for free.

  Supervisor   notices when a run() has returned and builds a replacement from
               a factory. Factories, not instances, precisely because a Thread
               is single-use.

Restart budgets are per worker and deliberately small. A worker that fails once
is usually transient; one that fails three times is a real problem, and looping
on it burns CPU and fills the log rather than fixing anything. The transcriber
and the command listener get one attempt, not three, because each restart
reloads a Whisper model -- seconds of CPU and hundreds of MB.
"""

from __future__ import annotations

import threading
from typing import Callable

from . import failure, log
from .bus import Bus, Suggestion


def guard(worker: threading.Thread, bus: Bus) -> threading.Thread:
    """Make `worker` report its own death instead of vanishing.

    `Thread.start()` calls `self.run()`, and an instance attribute shadows the
    class's bound method -- so replacing `worker.run` here changes what the
    thread executes without subclassing anything or editing any worker.
    """
    original = worker.run

    def guarded() -> None:
        try:
            original()
        except BaseException as exc:            # noqa: BLE001 - that is the point
            log.exception(worker.name, exc)
            bus.say(f"{worker.name}: crashed ({type(exc).__name__})")

    worker.run = guarded                        # type: ignore[method-assign]
    return worker


class Supervisor(threading.Thread):
    """Watches the workers and puts the restartable ones back."""

    daemon = True

    PERIOD = 5.0
    DEFAULT_LIMIT = 3

    def __init__(
        self,
        bus: Bus,
        specs: dict[str, Callable[[], threading.Thread]],
        limits: dict[str, int] | None = None,
        no_restart: tuple[str, ...] = (),
    ) -> None:
        super().__init__(name="supervisor")
        self.bus = bus
        self.specs = specs
        self.limits = limits or {}
        self.no_restart = no_restart
        self.live: dict[str, threading.Thread] = {}
        self.restarts: dict[str, int] = {name: 0 for name in specs}
        self.dead: set[str] = set()

    def start_all(self) -> None:
        """Build, guard and start every worker. Call instead of starting them."""
        for name, make in self.specs.items():
            worker = guard(make(), self.bus)
            self.live[name] = worker
            worker.start()

    def _limit(self, name: str) -> int:
        return self.limits.get(name, self.DEFAULT_LIMIT)

    def _report_dead(self, name: str) -> None:
        """Permanent failure. Has to be heard, not just logged."""
        if name in self.dead:
            return
        self.dead.add(name)
        log.say(f"{name}: gave up after {self.restarts[name]} restarts")
        self.bus.say(f"{name}: stopped for good")
        self.bus.suggestions.put(Suggestion(
            lead=f"{name} stopped working",
            points=["Nod is still running, but this part is not",
                    "Run Nod Check-up, then restart Nod"],
            kind="reply",
        ))
        self.bus.speech.put(failure.spoken(
            f"{name} stopped", "Part of me stopped working. Restarting Nod should fix it."))

    def run(self) -> None:
        while not self.bus.stop.wait(self.PERIOD):
            for name in self.specs:
                worker = self.live.get(name)
                if worker is None or worker.is_alive():
                    continue

                # A worker that decided to stop -- Tesseract absent, agent mode
                # never enabled -- is not a failure and must not be restarted in
                # a loop. The worker sets this before returning.
                if getattr(worker, "stopped_deliberately", False):
                    self.live.pop(name, None)
                    continue

                if name in self.no_restart:
                    self._report_dead(name)
                    self.live.pop(name, None)
                    continue

                if self.restarts[name] >= self._limit(name):
                    self._report_dead(name)
                    self.live.pop(name, None)
                    continue

                self.restarts[name] += 1
                log.say(f"{name}: died, restarting "
                        f"({self.restarts[name]}/{self._limit(name)})")
                self.bus.say(f"{name}: restarting "
                             f"({self.restarts[name]}/{self._limit(name)})")
                try:
                    fresh = guard(self.specs[name](), self.bus)
                    self.live[name] = fresh
                    fresh.start()
                except Exception as exc:
                    log.exception(f"restarting {name}", exc)
                    self._report_dead(name)
