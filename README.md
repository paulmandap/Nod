# Meeting Accessibility Copilot

Real-time meeting HUD: system audio → Whisper → LLM → transparent overlay,
with shared-screen OCR as a second input.

```
python -m copilot.main --list-devices     # find your loopback device
export GEMINI_API_KEY=...
python -m copilot.main --model small.en
```

Hotkeys: `Ctrl+Alt+H` hide/show · `Ctrl+Alt+Space` force refresh ·
`Ctrl+Alt+C` toggle click-through.

## Setup

```bash
pip install -r requirements.txt
```

Tesseract is a separate binary:

| OS | Install | Notes |
|---|---|---|
| Windows | UB-Mannheim installer | pass `--tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"` |
| macOS | `brew install tesseract` | |
| Linux | `apt install tesseract-ocr` | |

## Capturing system audio

This is the piece that eats hackathon hours, so budget for it.

| OS | How | Status |
|---|---|---|
| Windows | WASAPI loopback on the default speaker | works with no setup |
| Linux | PulseAudio/PipeWire `.monitor` source | works with no setup |
| macOS | **needs a virtual device** — install [BlackHole](https://github.com/ExistentialAudio/BlackHole), create a Multi-Output Device so you still hear the call, then `--audio-device "BlackHole 2ch"` | ~20 min of setup |

If you're demoing on a Mac, do this on day one, not the night before.

## Tuning

Latency is the product. If the HUD feels sluggish, in order of impact:

| Symptom | Knob | File |
|---|---|---|
| Transcription lags behind | `--model base.en`, or `--device cuda --compute-type float16` | `main.py` |
| Words clipped at chunk edges | raise `Segmenter.TAIL_BLOCKS` | `audio.py` |
| Triggers on keyboard/mouse noise | raise `START_RMS`, or swap in `webrtcvad` / Silero VAD | `audio.py` |
| CPU pegged | raise `--ocr-interval`, or `--no-vision` | `vision.py` |
| Garbage OCR | tune `_prep()` — try `--psm 4` for columns, `--psm 11` for sparse text | `vision.py` |
| HUD updates too often / too rarely | `MIN_INTERVAL`, `MIN_NEW_CHARS` | `llm.py` |
| 429s / HUD goes quiet | lower `RPM_LIMIT`, raise `MIN_INTERVAL` | `llm.py` |

**Expect ~2–4 s end to end** on a laptop CPU: ~1 s to detect end of speech,
~0.5–1.5 s for Whisper, ~0.7-1 s for Gemini Flash-Lite with thinking off. That is fine for "help me catch up"
and too slow for "tell me what to say next" — design the demo around the former.

## Architecture

Five daemon threads plus the Qt main thread, connected only by queues.

```
audio-capture ──> bus.audio ──> transcriber ──> bus.transcripts ─┐
                                                                 ├─> router
screen-reader ──────────────────────────────> bus.screen ────────┘    │
                                                                ContextStore
                                                                      │
                                                    suggester ────────┘
                                                          │
                                                  bus.suggestions
                                                          │
                                          HUD (main thread, QTimer poll)
```

Qt widgets can only be touched from the main thread, so the HUD polls its queue
on a `QTimer` rather than being called from a worker. `bus.stop` is a single
`threading.Event` every worker watches, which is what makes Ctrl+C clean.

`ContextStore` holds a 120-second rolling window — long enough to be useful,
short enough to keep prompts small and cheap.

## Design of the overlay

It's read for about a second, in peripheral vision, over an unpredictable
background. That drives every choice:

- **Dark smoked panel.** Stays legible over both white slide decks and dark
  IDEs; a light panel doesn't.
- **One strong line first.** Lead line is the fact itself ("Q3 launch slipped to
  November"), never a description of the discussion. Supporting points sit
  below a hairline so the eye can stop after the lead.
- **Almost no motion.** Animation at the top of the screen pulls the eye away
  from the camera, which defeats the purpose. Only a 140 ms crossfade on change.
- **The freshness rail.** The left edge drains over 45 seconds and greys out
  when stale. It answers "is this still current?" without costing a word of
  reading. Its colour also encodes kind — teal for summary, blue for a defined
  term, amber when a question is waiting on you.

## Before you demo

Meeting capture has real constraints worth having an answer ready for, since
judges usually ask:

- **Recording consent.** Several US states and most of the EU require all-party
  consent to record a conversation. Nothing here writes audio to disk, and
  saying so is a strong answer — consider keeping it that way and announcing
  the tool in the meeting.
- **Transcript retention.** Right now the transcript lives in a 120-second
  in-memory window and is never persisted. That's a feature; mention it.
- **What leaves the machine.** Whisper runs locally; only the rolling text
  window goes to the API. Worth stating explicitly on a slide.
- **Free-tier training.** Google's free tier permits using API inputs and
  outputs to improve their models; the paid tier does not. If you claim privacy
  on stage, say which tier you're on. Enabling billing on the key removes this,
  and Flash-Lite traffic at this volume costs cents.
