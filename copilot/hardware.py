"""What Nod will use to speak and be seen, resolved before it joins anything.

The point of this module is the card that goes up before a call: "microphone —
Razer Barracuda X, camera — REDRAGON Live Camera". Joining a meeting on the
wrong device is the failure that costs the first two minutes of every call, and
it is the one thing you cannot check from inside the meeting once you are in it
muted.

Cameras come from a WMI query rather than OpenCV. Probing indices with
`cv2.VideoCapture(i)` is the usual trick and it is a bad one here: it opens the
device, which turns the recording light on. A tally light blinking while Nod
merely takes inventory is exactly the wrong impression to give.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import List

import soundcard as sc

_PS_CAMERAS = r"""
Get-CimInstance Win32_PnPEntity |
  Where-Object { $_.PNPClass -eq 'Camera' -or $_.Service -eq 'usbvideo' } |
  Select-Object -ExpandProperty Name |
  ConvertTo-Json -Compress
"""


@dataclass
class Hardware:
    microphone: str | None = None
    speaker: str | None = None
    camera: str | None = None
    cameras: List[str] = field(default_factory=list)

    def card_points(self) -> List[str]:
        """The two or three lines the HUD shows before joining."""
        points = []
        if self.microphone:
            points.append(f"Microphone — {_plain(self.microphone)}")
        points.append(f"Camera — {_plain(self.camera)}" if self.camera
                      else "Camera — none detected")
        points.append("Joining muted, camera off")
        return points

    def spoken(self) -> str:
        mic = self.microphone or "no microphone"
        cam = self.camera or "no camera"
        return f"Using {_plain(mic)} for microphone and {_plain(cam)} for camera."


def _plain(name: str) -> str:
    """Device names are written for a settings dialog, not for a voice.

    "Microphone (Razer Barracuda X 2.4)" read aloud verbatim is a mouthful with
    a bracket in the middle, so strip the wrapper down to the product.
    """
    name = name.replace("Microphone (", "").replace("Speakers (", "")
    name = name.rstrip(")")
    for noise in (" 2.4", " Audio", " Live Camera Audio"):
        name = name.replace(noise, "")
    return name.strip()


def cameras() -> List[str]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_CAMERAS],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        if not out:
            return []
        found = json.loads(out)
        return [found] if isinstance(found, str) else list(found)
    except Exception:
        return []


# Enumerating cameras spawns a PowerShell, which costs the better part of a
# second -- paid on the agent thread, every time a command touches devices, and
# "attend my meeting" touches them twice. Devices change when hardware is
# plugged in, which the RUNBOOK already says requires a restart (the audio
# device is bound at startup anyway), so a short TTL is consistent with how the
# rest of the app behaves.
_CACHE_TTL = 60.0
_cache: dict[tuple, tuple[float, Hardware]] = {}
_cache_lock = threading.Lock()


def detect(prefer_camera: str | None = None,
           prefer_mic: str | None = None,
           refresh: bool = False) -> Hardware:
    key = (prefer_camera, prefer_mic)
    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return hit[1]

    hw = _detect_uncached(prefer_camera, prefer_mic)
    with _cache_lock:
        _cache[key] = (time.monotonic(), hw)
    return hw


def _detect_uncached(prefer_camera: str | None,
                     prefer_mic: str | None) -> Hardware:
    hw = Hardware()

    try:
        hw.microphone = (sc.get_microphone(prefer_mic).name if prefer_mic
                         else sc.default_microphone().name)
    except Exception:
        hw.microphone = None

    try:
        hw.speaker = sc.default_speaker().name
    except Exception:
        hw.speaker = None

    hw.cameras = cameras()
    if prefer_camera:
        hw.camera = next((c for c in hw.cameras
                          if prefer_camera.lower() in c.lower()), None)
    if not hw.camera and hw.cameras:
        hw.camera = hw.cameras[0]

    return hw


if __name__ == "__main__":
    hw = detect()
    print("microphone:", hw.microphone)
    print("speaker   :", hw.speaker)
    print("cameras   :", hw.cameras)
    print()
    for p in hw.card_points():
        print("  ", p)
    print()
    print("spoken:", hw.spoken())
