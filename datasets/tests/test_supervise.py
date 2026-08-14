"""Workers that die must come back, or at least say that they did not.

Three threads used to end with a bare `return` on ordinary first-run problems
-- no model downloaded, no Tesseract installed, one malformed queue item -- and
a Python thread cannot be restarted once run() has returned. The app kept
running, the overlay kept saying MEETING, and nothing worked. On a tester's
machine that is the entire bug report: "it just stopped doing anything".

What is asserted here:

  * a crash is caught, logged and announced instead of vanishing
  * a dead worker is rebuilt from its factory
  * the restart budget is respected, and running out is reported once
  * `stopped_deliberately` means "I chose to stop", and is never restarted --
    otherwise a machine without Tesseract restarts the screen reader every
    five seconds forever

    python datasets\tests\test_supervise.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copilot.bus import Bus                                  # noqa: E402
from copilot.supervise import Supervisor, guard              # noqa: E402


def check(name: str, ok: bool, note: str = "") -> int:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + note if note else ''}")
    return 0 if ok else 1


def drain(bus: Bus) -> list[str]:
    out = []
    while not bus.status.empty():
        out.append(bus.status.get_nowait())
    return out


class Boom(threading.Thread):
    """A worker that raises immediately."""

    daemon = True

    def __init__(self, counter):
        super().__init__(name="boom")
        self.counter = counter

    def run(self):
        self.counter.append(1)
        raise RuntimeError("planned failure")


class Quitter(threading.Thread):
    """A worker that decides to stop, the way ScreenReader does without OCR."""

    daemon = True

    def __init__(self, counter):
        super().__init__(name="quitter")
        self.counter = counter
        self.stopped_deliberately = False

    def run(self):
        self.counter.append(1)
        self.stopped_deliberately = True


def main() -> int:
    bad = 0

    print("guard() turns a silent death into a reported one")
    bus = Bus()
    started: list[int] = []
    worker = guard(Boom(started), bus)
    worker.start()
    worker.join(2.0)
    said = drain(bus)
    bad += check("the exception did not escape", not worker.is_alive())
    bad += check("it was announced on the bus",
                 any("boom" in s and "crashed" in s for s in said), str(said))

    print("\nA dead worker is rebuilt from its factory")
    bus = Bus()
    started = []
    sup = Supervisor(bus, {"boom": lambda: Boom(started)}, limits={"boom": 2})
    sup.PERIOD = 0.1
    sup.start_all()
    sup.start()
    deadline = time.time() + 5
    while time.time() < deadline and len(started) < 3:
        time.sleep(0.05)
    # Wait for the poll *after* the last failure: giving up is decided when the
    # supervisor next notices the thread is gone, not at the moment it dies.
    deadline = time.time() + 3
    while time.time() < deadline and "boom" not in sup.dead:
        time.sleep(0.05)
    bus.stop.set()
    sup.join(2.0)
    # 1 original start + 2 permitted restarts, and no more.
    bad += check("restarted up to the limit", len(started) == 3,
                 f"started {len(started)}x")
    said = drain(bus)
    bad += check("each restart was announced",
                 sum("restarting" in s for s in said) == 2, str(said))
    bad += check("giving up was announced once",
                 sum("stopped for good" in s for s in said) == 1, str(said))

    print("\nA deliberate stop is not a crash")
    bus = Bus()
    started = []
    sup = Supervisor(bus, {"quitter": lambda: Quitter(started)})
    sup.PERIOD = 0.1
    sup.start_all()
    sup.start()
    time.sleep(1.0)
    bus.stop.set()
    sup.join(2.0)
    bad += check("never restarted", len(started) == 1, f"started {len(started)}x")
    said = drain(bus)
    bad += check("and never reported as dead",
                 not any("stopped for good" in s for s in said), str(said))

    print("\nno_restart workers are reported, not rebuilt")
    bus = Bus()
    started = []
    sup = Supervisor(bus, {"boom": lambda: Boom(started)},
                     no_restart=("boom",))
    sup.PERIOD = 0.1
    sup.start_all()
    sup.start()
    time.sleep(1.0)
    bus.stop.set()
    sup.join(2.0)
    bad += check("started exactly once", len(started) == 1,
                 f"started {len(started)}x")
    said = drain(bus)
    bad += check("reported as stopped",
                 any("stopped for good" in s for s in said), str(said))

    print("\nA healthy worker is left alone")
    bus = Bus()
    alive = threading.Event()

    def forever():
        alive.set()
        bus.stop.wait(30)

    builds = []

    def make():
        builds.append(1)
        return threading.Thread(target=forever, name="steady", daemon=True)

    sup = Supervisor(bus, {"steady": make})
    sup.PERIOD = 0.1
    sup.start_all()
    sup.start()
    time.sleep(1.0)
    bus.stop.set()
    sup.join(2.0)
    bad += check("built once, never restarted", len(builds) == 1,
                 f"built {len(builds)}x")

    print(f"\nFAILURES: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
