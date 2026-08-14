"""System-audio (loopback) capture.

Capturing what the *speakers* are playing is platform-specific:

  Windows  WASAPI loopback. Works out of the box via `soundcard`.
  Linux    PulseAudio/PipeWire monitor source. Works via `soundcard`.
  macOS    No OS-level loopback. Install a virtual device (BlackHole 2ch or
           Rogue Amoeba Loopback), route the meeting app's output into it,
           then pass --audio-device "BlackHole 2ch".

Run `python -m copilot.audio` to list device names.
"""

from __future__ import annotations

import threading

import numpy as np
import soundcard as sc

from .bus import AudioBlock, Bus

SAMPLE_RATE = 16_000          # Whisper's native rate; no resampling downstream
BLOCK_FRAMES = 1600           # 100 ms per block


def list_devices() -> None:
    print("Speakers (use these names for loopback):")
    for s in sc.all_speakers():
        print(f"  {s.name}")
    print("\nInput devices:")
    for m in sc.all_microphones(include_loopback=True):
        print(f"  {m.name}")


class LoopbackCapture(threading.Thread):
    """Pushes 100 ms mono float32 blocks onto bus.audio."""

    daemon = True

    def __init__(self, bus: Bus, device_name: str | None = None) -> None:
        super().__init__(name="audio-capture")
        self.bus = bus
        self.device_name = device_name

    def _open(self):
        if self.device_name:
            return sc.get_microphone(self.device_name, include_loopback=True)
        # Default: loop back whatever the default speaker is playing.
        return sc.get_microphone(sc.default_speaker().name, include_loopback=True)

    def run(self) -> None:
        try:
            mic = self._open()
        except Exception as exc:
            self.bus.say(f"audio: no loopback device ({exc})")
            return

        self.bus.say(f"audio: {mic.name}")
        try:
            with mic.recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK_FRAMES) as rec:
                while not self.bus.stop.is_set():
                    # Meeting listening off: hold the device open but publish
                    # nothing. Reopening a WASAPI loopback on every toggle is
                    # slower and occasionally fails outright.
                    if not self.bus.meeting_on.is_set():
                        if not self.bus.idle(self.bus.meeting_on):
                            break
                        continue
                    data = rec.record(numframes=BLOCK_FRAMES)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    self.bus.put_drop_oldest(
                        self.bus.audio, AudioBlock(data.astype(np.float32))
                    )
        except Exception as exc:
            self.bus.say(f"audio: stopped ({exc})")


class Segmenter:
    """Cuts the continuous stream into utterances on silence.

    Whisper is not a streaming model — it wants a complete chunk. Feeding it
    fixed 5-second windows chops words in half, so instead we cut on silence
    with hysteresis, and force a cut if someone talks past MAX_UTTERANCE so the
    HUD never goes quiet during a monologue.

    RMS thresholding is crude but has no model-loading cost. Swap in
    `webrtcvad` or Silero VAD if you get false triggers from keyboard noise.

    One caveat worth knowing, because it is invisible until you look: silence
    cutting assumes a quiet room. Media playback — a video with a music bed, a
    stream with backing audio — never drops below STOP_RMS for HANG_BLOCKS in a
    row, so *every* cut comes from MAX_UTTERANCE instead. Measured on a 45 s
    YouTube clip: one qualifying silence gap, total. That makes MAX_UTTERANCE
    the real latency knob for anything other than a meeting, which is why it is
    8 s and not the 12 s that a conference call can afford.
    """

    START_RMS = 0.010          # energy to consider speech started
    STOP_RMS = 0.006           # lower bar to keep it going (hysteresis)
    HANG_BLOCKS = 7            # ~700 ms of quiet ends the utterance
    MIN_UTTERANCE = 8          # ignore anything under ~0.8 s
    MAX_UTTERANCE = 80         # force a cut at ~8 s
    TAIL_BLOCKS = 3            # carry 300 ms forward so words aren't clipped

    def __init__(self) -> None:
        self._buf: list[np.ndarray] = []
        self._tail: list[np.ndarray] = []
        self._quiet = 0
        self._active = False

    def push(self, block: np.ndarray) -> np.ndarray | None:
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)

        if not self._active:
            self._tail.append(block)
            self._tail = self._tail[-self.TAIL_BLOCKS:]
            if rms > self.START_RMS:
                self._active = True
                self._buf = list(self._tail)
                self._tail = []
                self._quiet = 0
            return None

        self._buf.append(block)
        self._quiet = self._quiet + 1 if rms < self.STOP_RMS else 0

        too_long = len(self._buf) >= self.MAX_UTTERANCE
        if self._quiet >= self.HANG_BLOCKS or too_long:
            audio = np.concatenate(self._buf)
            long_enough = len(self._buf) >= self.MIN_UTTERANCE
            self._tail = self._buf[-self.TAIL_BLOCKS:] if too_long else []
            self._buf = []
            self._active = too_long
            self._quiet = 0
            return audio if long_enough else None
        return None


if __name__ == "__main__":
    list_devices()
