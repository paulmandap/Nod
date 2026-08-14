"""Driving a browser into a Google Meet call, muted and dark.

Brave by default, falling back to Chrome then Edge; `NOD_BROWSER` forces a
specific binary. All three are Chromium, so the DevTools protocol and every
flag below are identical -- only the executable differs.

There is no API for this. Google exposes meeting *creation*; making a
particular human attend is not something it offers, so the only route is to
drive a real browser through the same pre-join screen a person would click.
That makes this the most fragile file in the project by a wide margin -- it
depends on Meet's DOM, which Google reshuffles and A/B tests. Everything below
is written to fail loudly and legibly rather than to silently half-work.

Chrome runs against its own profile directory under ~/.nod/chrome rather than
your everyday one. Two reasons, and the first is not optional: you cannot add
a debugging port to an already-running Chrome, and pointing a new launch at a
profile that is already open just hands off to the existing window and drops
the flag. Requiring you to close every Chrome window first would be a miserable
way to start a meeting. The second reason is that automation gets its own
cookie jar and cannot wander around a session you are using.

You sign into that profile once, the first time. After that it persists.

Mic and camera are turned off on the pre-join screen *before* joining, not
after. Joining first and muting second means a room full of people hears
whatever your microphone picked up in between.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests

NOD_HOME = Path(os.environ.get("NOD_HOME", Path.home() / ".nod"))
# Named for what it is (a browser profile) rather than for Brave or Chrome --
# which one gets launched is decided at runtime, and the profile is not
# portable between them, so switching browsers means signing in again.
PROFILE = NOD_HOME / "browser"
# Chrome only honours --remote-debugging-port on the launch that actually
# starts the profile; a second launch against a live profile hands off to the
# running window and drops the flag, so the port never opens and the failure
# looks like "Chrome did not start". Remembering the port lets a later run
# reattach instead of fighting it.
PORT_FILE = NOD_HOME / "chrome-port"

# Brave first. It is Chromium, so it speaks the same DevTools protocol and
# every flag below applies unchanged -- the only thing that differs is which
# binary gets launched. Set NOD_BROWSER to a full path to force one.
BROWSER_CANDIDATES = (
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    str(Path.home() / r"AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


class MeetError(RuntimeError):
    pass


@dataclass
class JoinResult:
    ok: bool
    detail: str
    muted: bool = False
    camera_off: bool = False
    # What actually happened, as opposed to whether the click landed.
    # "waiting" means the request went in and Meet is holding us in the waiting
    # room -- which is not being in the meeting, and used to be reported as if
    # it were.
    state: str = "failed"          # joined | waiting | failed
    # The DevTools target id of the page this opened, when the caller may want
    # to close it later. Used by the music path so a second song replaces the
    # first instead of playing over it.
    target_id: str = ""


# --- the smallest CDP client that does the job -------------------------------

class Tab:
    """One Chrome tab, driven over the DevTools protocol."""

    def __init__(self, ws_url: str) -> None:
        import websocket
        # Chrome rejects a DevTools websocket carrying a browser-ish Origin
        # header, which websocket-client sends by default. The usual advice is
        # to launch with --remote-allow-origins=*, but that tells Chrome to
        # accept debugger connections from anywhere; simply not sending the
        # header keeps the check intact and satisfies it.
        self._ws = websocket.create_connection(ws_url, timeout=30,
                                               suppress_origin=True)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method,
                                  "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise MeetError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            # Anything else is an event we did not subscribe to; ignore it.

    def js(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            raise MeetError(result["exceptionDetails"].get("text", "js error"))
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browser_path() -> str:
    override = os.environ.get("NOD_BROWSER", "").strip('" ')
    if override:
        if not Path(override).exists():
            raise MeetError(f"NOD_BROWSER does not exist: {override}")
        return override

    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    for name in ("brave", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise MeetError("no Chromium browser found")


def browser_name() -> str:
    """For status lines: 'Brave', 'Chrome', 'Edge'."""
    try:
        stem = Path(browser_path()).stem.lower()
    except MeetError:
        return "browser"
    return {"brave": "Brave", "chrome": "Chrome", "msedge": "Edge"}.get(stem, stem)


class Browser:
    """A Chrome launched with a debugging port, on Nod's own profile."""

    def is_alive(self) -> bool:
        """Is the Chrome this object refers to still there?

        Worth calling before reusing a cached Browser: closing the window is a
        perfectly normal thing for a user to do, and a handle to a dead one
        fails with a ConnectionError naming a port number, which is neither
        actionable nor pleasant to hear read aloud.
        """
        return self._alive(self.port)

    @staticmethod
    def _alive(port: int) -> bool:
        try:
            requests.get(f"http://127.0.0.1:{port}/json/version",
                         timeout=1).raise_for_status()
            return True
        except Exception:
            return False

    def __init__(self, headless: bool = False) -> None:
        PROFILE.mkdir(parents=True, exist_ok=True)

        # Reattach to a Nod Chrome that is already up, rather than launching a
        # second one that would just hand off and lose the debugging port.
        if PORT_FILE.exists():
            try:
                existing = int(PORT_FILE.read_text().strip())
            except ValueError:
                existing = 0
            if existing and self._alive(existing):
                self.port = existing
                self.proc = None
                return

        self.port = _free_port()
        args = [
            browser_path(),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            # --use-fake-ui-for-media-stream used to be here. It made Chrome
            # auto-accept every getUserMedia request for the life of the
            # process, which solved the Meet lobby and created a much worse
            # problem: the same browser is reused by "open youtube", "open
            # facebook", "open teams". Any of those pages, or anything they
            # link to, could turn the camera and microphone on with no
            # permission bar and no way for the user to know.
            #
            # Permission is now granted per origin over the DevTools protocol
            # instead -- see Browser.grant_media -- so Meet gets a microphone,
            # nothing gets a camera, and every other site gets Chrome's normal
            # prompt.
            # Without this, YouTube Music loads the track and refuses to start
            # it: Chrome blocks play() that no click led to, so the page sits
            # at readyState 4, paused, forever. There is no gesture to give it
            # when the whole point is hands-free.
            "--autoplay-policy=no-user-gesture-required",
        ]
        if headless:
            args.append("--headless=new")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._wait_ready()
        PORT_FILE.write_text(str(self.port), encoding="utf-8")

    def grant_media(self, origin: str, camera: bool = False) -> bool:
        """Allow `origin` a microphone, and by default deny it a camera.

        This is the strongest of the four camera protections, and the only one
        that does not depend on reading Google's HTML. Browser.grantPermissions
        is a whitelist: every permission *not* listed is denied outright, so
        with camera=False a getUserMedia({video: true}) fails inside Chrome.
        The capture device is never opened, which means the hardware indicator
        light never comes on -- regardless of what the page's markup says, what
        language it is in, or whether Meet redesigns the lobby tomorrow.

        Returns False rather than raising: if this fails, join() still has the
        DOM checks and the live-track check to fall back on, and refusing to
        join because a hardening step could not be applied would be worse than
        joining with the other three in place.
        """
        try:
            version = requests.get(
                f"http://127.0.0.1:{self.port}/json/version", timeout=5).json()
            ws = Tab(version["webSocketDebuggerUrl"])
        except Exception:
            return False

        try:
            perms = ["audioCapture"] + (["videoCapture"] if camera else [])
            ws.call("Browser.grantPermissions", origin=origin, permissions=perms)
            return True
        except Exception:
            return False
        finally:
            ws.close()

    def close_target(self, target_id: str) -> None:
        """Close one tab. Used to stop the previous song before starting another."""
        if not target_id:
            return
        try:
            requests.get(f"http://127.0.0.1:{self.port}/json/close/{target_id}",
                         timeout=5)
        except Exception:
            pass

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._alive(self.port):
                return
            if self.proc and self.proc.poll() is not None:
                raise MeetError(
                    "Chrome exited immediately — most likely another Chrome is "
                    f"already holding {PROFILE}"
                )
            time.sleep(0.3)
        raise MeetError("Chrome did not open its debugging port")

    def open(self, url: str) -> Tab:
        target = requests.put(
            f"http://127.0.0.1:{self.port}/json/new?{url}", timeout=10
        )
        if target.status_code >= 400:      # older Chrome builds want GET
            target = requests.get(
                f"http://127.0.0.1:{self.port}/json/new?{url}", timeout=10)
        target.raise_for_status()
        info = target.json()
        tab = Tab(info["webSocketDebuggerUrl"])
        # Carried so a caller can close the page later. tab.close() only drops
        # the websocket -- the tab itself stays open, which is right for a
        # meeting and wrong for the previous song.
        tab.target_id = info.get("id", "")
        tab.call("Runtime.enable")
        tab.call("Page.enable")
        return tab

    def signed_in(self) -> bool:
        return (PROFILE / "Default" / "Cookies").exists()


# --- the Meet pre-join screen ------------------------------------------------

# Finding controls by aria-label rather than class name: Meet's classes are
# minified and change constantly, while the labels are accessibility surface
# and change far more slowly. Several spellings are tried because the label
# differs by locale and by whether the control is currently on or off.
_JS_HELPERS = r"""
window.__nod = {
  buttons() { return Array.from(document.querySelectorAll('button,[role=button]')); },

  // Matches on aria-label and data-tooltip only. textContent used to be in
  // here, and it is why a non-toggle element could win: the Meet lobby shows
  // captions like "Your camera is off", which contain the word "camera" and
  // come earlier in the DOM than the real control. Clicking one of those does
  // nothing useful, and the state read back afterwards belongs to the wrong
  // element entirely. Where several candidates match, prefer one carrying
  // data-is-muted, because only the genuine toggle has it.
  byLabel(words) {
    const hits = this.buttons().filter(b => {
      const l = ((b.getAttribute('aria-label') || '') + ' ' +
                 (b.getAttribute('data-tooltip') || '')).toLowerCase();
      return l && words.some(w => l.includes(w));
    });
    return hits.find(b => b.hasAttribute('data-is-muted')) || hits[0] || null;
  },

  // Three states, and "unknown" is the important one.
  //
  // This used to return a boolean derived from the English string "turn on",
  // while the word lists passed to byLabel were localised into Spanish and
  // Filipino. On a Meet UI in either of those, a camera that was already off
  // read as on -- so the code helpfully clicked it, turning the camera ON
  // moments before joining. A boolean has nowhere to put "I could not tell",
  // so it guessed, and guessing wrong here points a webcam at someone.
  OFF_WORDS: ['turn on', 'activar', 'encender', 'i-on', 'buksan',
              'einschalten', 'activer', 'ligar', 'attiva', 'aan', 'włącz'],
  ON_WORDS:  ['turn off', 'desactivar', 'apagar', 'i-off', 'patayin',
              'ausschalten', 'désactiver', 'desligar', 'disattiva', 'uit',
              'wyłącz'],

  deviceState(b) {
    if (!b) return 'unknown';
    const muted = b.getAttribute('data-is-muted');
    if (muted !== null) return muted === 'true' ? 'off' : 'on';
    const l = (b.getAttribute('aria-label') || '').toLowerCase();
    // A label offering to turn it ON means it is currently OFF.
    if (this.OFF_WORDS.some(w => l.includes(w))) return 'off';
    if (this.ON_WORDS.some(w => l.includes(w)))  return 'on';
    return 'unknown';
  },

  // A second opinion that reads no labels at all, so no locale and no markup
  // change can fool it: is a video track actually running in this page?
  cameraLive() {
    return Array.from(document.querySelectorAll('video')).some(v =>
      v.srcObject && typeof v.srcObject.getVideoTracks === 'function' &&
      v.srcObject.getVideoTracks().some(
        t => t.readyState === 'live' && t.enabled && !t.muted));
  },

  // Post-join proof. Participant tiles carry data-participant-id, which is not
  // localised and is only present once actually in the call.
  inCall() { return !!document.querySelector('[data-participant-id]'); }
};
"""

MIC_WORDS = ["microphone", "micrófono", "mikropono", "mikrofon", "micro"]
CAM_WORDS = ["camera", "cámara", "kamera", "caméra", "videocamera"]
JOIN_WORDS = ["join now", "ask to join", "sumali ngayon", "participar",
              "unirse ahora", "jetzt teilnehmen"]


def _ensure_off(tab: Tab, words: list[str]) -> str:
    """Switch a device off if it is on. Returns 'off', 'on' or 'unknown'.

    Never clicks on 'unknown'. That single rule is the fix for the locale bug:
    when the state cannot be read, the safe action is to leave the control
    alone and let the caller refuse to join, not to toggle it and hope.
    """
    tab.js(_JS_HELPERS)
    words_js = json.dumps(words)
    state = tab.js(f"(() => {{ const b = window.__nod.byLabel({words_js});"
                   f" return window.__nod.deviceState(b); }})()")

    if state in ("off", "unknown"):
        return state

    tab.js(f"(() => {{ const b = window.__nod.byLabel({words_js});"
           f" if (b) b.click(); return true; }})()")
    time.sleep(0.6)
    return tab.js(f"(() => {{ const b = window.__nod.byLabel({words_js});"
                  f" return window.__nod.deviceState(b); }})()")


def _confirm_joined(tab: Tab, timeout: float = 30.0) -> str:
    """Did the click actually get us in? Returns 'joined', 'waiting' or 'failed'.

    Clicking the button used to be treated as success, so Nod said "You're in,
    muted and camera off" when it had landed in a waiting room, when the call
    had ended, or when Meet showed "You can't join this call". A confident false
    statement about whether you are in a meeting is worse than silence.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if tab.js("window.__nod.inCall()"):
                return "joined"
            words_js = json.dumps(JOIN_WORDS)
            if not tab.js(f"!!window.__nod.byLabel({words_js})"):
                # The join button is gone but no participant tiles exist: we
                # asked to join and are being held in the waiting room.
                return "waiting"
        except Exception:
            return "failed"
        time.sleep(1.0)
    return "failed"


def join(url: str, browser: Browser | None = None,
         timeout: float = 45.0, require_camera_off: bool = True) -> JoinResult:
    """Open `url`, silence both devices, then join.

    Never joins with the microphone live, and by default never joins without
    having confirmed the camera is off either. `require_camera_off=False`
    restores the old behaviour of joining anyway and reporting it.
    """
    own = browser is None
    browser = browser or Browser()
    tab = None
    try:
        # Deny the camera at the browser before the page can ask for it. Layer
        # one of four; see Browser.grant_media.
        browser.grant_media("https://meet.google.com", camera=False)

        tab = browser.open(url)

        # Wait for the lobby to render a device control, which is the only
        # reliable signal that the pre-join screen is actually up.
        deadline = time.time() + timeout
        ready = False
        mic_js = json.dumps(MIC_WORDS)
        while time.time() < deadline:
            tab.js(_JS_HELPERS)
            if tab.js(f"!!window.__nod.byLabel({mic_js})"):
                ready = True
                break
            if tab.js("document.body ? document.body.innerText.toLowerCase()"
                      ".includes('sign in') : false"):
                return JoinResult(False, "Chrome profile is not signed in to Google")
            time.sleep(0.7)

        if not ready:
            return JoinResult(False, "pre-join screen never appeared")

        mic = _ensure_off(tab, MIC_WORDS)
        cam = _ensure_off(tab, CAM_WORDS)
        muted = mic == "off"
        cam_off = cam == "off"

        if not muted:
            return JoinResult(False,
                              f"did not join: could not confirm the microphone "
                              f"is off ({mic})", muted, cam_off)

        # The camera gate, and it sits BEFORE the join click rather than after
        # it. Reporting an unexpected camera afterwards still means several
        # seconds of live video going out to a room of people, which is not a
        # recoverable mistake -- the alternative, missing a meeting, is.
        live = False
        try:
            live = bool(tab.js("window.__nod.cameraLive()"))
        except Exception:
            live = False

        if require_camera_off and (not cam_off or live):
            reason = "the camera is still on" if live else f"camera state is {cam}"
            return JoinResult(False,
                              f"did not join: could not confirm the camera is "
                              f"off ({reason})", muted, False)

        words_js = json.dumps(JOIN_WORDS)
        clicked = tab.js(f"(() => {{ const b = window.__nod.byLabel({words_js});"
                         f" if (b) {{ b.click(); return true; }} return false; }})()")
        if not clicked:
            return JoinResult(False, "no join button found", muted, cam_off)

        state = _confirm_joined(tab)
        if state == "failed":
            return JoinResult(False, "clicked join, but never got into the call",
                              muted, cam_off)

        # Devices can reset as the call page takes over from the lobby, so the
        # state that matters is the one after the transition, not before it.
        cam_after = _ensure_off(tab, CAM_WORDS)
        if cam_after == "on":
            cam_off = False
        elif cam_after == "off":
            cam_off = True

        if state == "waiting":
            return JoinResult(True, "waiting to be let in", muted, cam_off,
                              state="waiting")
        if not cam_off:
            return JoinResult(True, "joined, but the camera may be on",
                              muted, cam_off, state="joined")
        return JoinResult(True, "joined", muted, cam_off, state="joined")

    except Exception as exc:
        return JoinResult(False, f"{type(exc).__name__}: {exc}")
    finally:
        if tab:
            tab.close()
        if own:
            pass       # leave Chrome open: closing it would leave the meeting


# --- YouTube Music -----------------------------------------------------------

# Picked for breadth rather than taste: with no request to go on, something has
# to be chosen, and a fixed default would play the same song every time.
SHUFFLE_SEEDS = (
    "lofi hip hop radio", "jazz classics", "acoustic covers", "90s opm hits",
    "chill electronic", "classic rock anthems", "bossa nova", "synthwave",
)


def play_music(query: str = "", browser: Browser | None = None,
               timeout: float = 30.0) -> JoinResult:
    """Search YouTube Music and start the first result playing.

    Uses the search page rather than a playlist URL because a playlist that is
    personal to an account stops working the moment the profile is signed out,
    and this profile may well be. Search works signed out.

    Like the Meet join, this depends on someone else's DOM and will need
    revisiting when YouTube Music changes. It reports which step failed.
    """
    import random
    import urllib.parse

    term = query.strip() or random.choice(SHUFFLE_SEEDS)
    url = ("https://music.youtube.com/search?q="
           + urllib.parse.quote(term))

    own = browser is None
    browser = browser or Browser()
    tab = None
    try:
        tab = browser.open(url)

        deadline = time.time() + timeout
        started = False
        while time.time() < deadline:
            # ytmusic-*-renderer elements are the search result rows. Clicking
            # the title of the first one starts playback.
            # Prefer a row whose subtitle says "Song". A bare search for a
            # famous track puts lyric videos, karaoke versions and live
            # uploads above the recording itself, so taking the first result
            # reliably plays the wrong thing.
            clicked = tab.js(r"""
            (() => {
              const rows = Array.from(document.querySelectorAll(
                'ytmusic-responsive-list-item-renderer'));
              if (!rows.length) return false;
              const isSong = r => {
                const f = r.querySelector('.secondary-flex-columns, .flex-column');
                return f && /(^|\s)Song(\s|$|•)/i.test(f.textContent);
              };
              const pick = rows.find(isSong) || rows[0];
              const pb = pick.querySelector('ytmusic-play-button-renderer');
              if (pb) { pb.click(); return true; }
              const a = pick.querySelector('a');
              if (a) { a.click(); return true; }
              return false;
            })()
            """)
            if clicked:
                started = True
                break
            time.sleep(0.8)

        if not started:
            return JoinResult(False, f"no playable result for '{term}'")

        # Give the watch page time to swap in, then confirm it is really
        # advancing. currentTime moving is the only trustworthy signal --
        # `paused === false` flips before any audio comes out.
        playing = False
        for _ in range(10):
            time.sleep(1.0)
            playing = tab.js(
                "(() => { const v = document.querySelector('video');"
                " if (!v) return false;"
                " if (v.paused) { v.play().catch(()=>{}); }"
                " return !v.paused && v.currentTime > 0.4; })()"
            )
            if playing:
                break
        now = tab.js(
            "(() => { const t = document.querySelector("
            "'.title.ytmusic-player-bar, yt-formatted-string.title');"
            " return t ? t.textContent.trim() : ''; })()"
        ) or term

        # The tab id rides back on the result so the agent can close this page
        # before starting the next song. Without it every request opened a new
        # tab and left the old one playing, so asking for a second song gave
        # you both at once -- through the speakers, which the meeting capture
        # is recording and transcribing.
        return JoinResult(bool(playing), f"playing {now}" if playing
                          else f"opened '{term}' but playback did not start",
                          target_id=getattr(tab, "target_id", ""))
    except Exception as exc:
        return JoinResult(False, f"{type(exc).__name__}: {exc}")
    finally:
        if tab:
            tab.close()


def probe(url: str) -> int:
    """Dump every control on a page, so the selectors can be re-checked fast.

    Everything in _JS_HELPERS matches against Google's markup, which is theirs
    to change and impossible to verify from a development session. When a join
    starts failing, this is the difference between an afternoon of guessing and
    thirty seconds:

        python -m copilot.meet --probe https://meet.google.com/xxx-xxxx-xxx

    Opens the page in Nod's own browser profile with the same media permissions
    the real join uses, waits for you to reach the lobby, then prints the
    aria-label, tooltip, mute attribute and text of every button it can see.
    """
    browser = Browser()
    browser.grant_media("https://meet.google.com", camera=False)
    tab = browser.open(url)
    try:
        print("Waiting 8s for the page to settle...")
        time.sleep(8)
        tab.js(_JS_HELPERS)
        rows = tab.js("""
        (() => window.__nod.buttons().map(b => ({
            label:   b.getAttribute('aria-label') || '',
            tooltip: b.getAttribute('data-tooltip') || '',
            muted:   b.getAttribute('data-is-muted'),
            text:    (b.textContent || '').trim().slice(0, 40)
        })).filter(r => r.label || r.tooltip || r.text))()
        """) or []

        print(f"\n{len(rows)} controls found\n")
        print(f"{'aria-label':<38} {'data-tooltip':<22} {'muted':<7} text")
        print("-" * 100)
        for r in rows:
            print(f"{r['label'][:37]:<38} {r['tooltip'][:21]:<22} "
                  f"{str(r['muted']):<7} {r['text']}")

        print("\nWhat Nod would decide right now:")
        for name, words in (("microphone", MIC_WORDS), ("camera", CAM_WORDS),
                            ("join button", JOIN_WORDS)):
            words_js = json.dumps(words)
            found = tab.js(f"!!window.__nod.byLabel({words_js})")
            state = tab.js(f"(() => {{ const b = window.__nod.byLabel({words_js});"
                           f" return window.__nod.deviceState(b); }})()")
            print(f"  {name:<12} found={found}  state={state}")
        print(f"  camera live  {tab.js('window.__nod.cameraLive()')}")
        print(f"  in call      {tab.js('window.__nod.inCall()')}")
        return 0
    finally:
        tab.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="copilot.meet")
    ap.add_argument("--probe", metavar="URL",
                    help="dump the controls Nod can see on a Meet page")
    ns = ap.parse_args()
    if ns.probe:
        raise SystemExit(probe(ns.probe))
    ap.print_help()
