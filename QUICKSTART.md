# Nod — Quick Start

Everything you need to use it. For the technical detail, see `RUNBOOK.md`.

---

## 1. Start it

Open PowerShell and paste:

```powershell
cd C:\paul\Nod
.\.venv\Scripts\python.exe -m copilot.main --agent --local-intent --no-summary --camera REDRAGON
```

- Wait for the overlay to show **`AGENT   EARS: SAY 'HEY NOD'`**
- Until it says that, it can't hear you
- Leave the terminal window open — closing it stops Nod

---

## 2. Talk to it

Say **"Hey Nod"**, wait for **"Yes, boss?"**, then say what you want.

Or say it all in one breath: *"Hey Nod, play me some music."*

### What you can say

**Ask anything**
- *"Hey Nod, what's the exchange rate for dollars to pesos?"*
- *"Hey Nod, how do I fry an egg?"*
- *"Hey Nod, who is the president of the Philippines?"*

**Change how it explains**
- *"Explain it like you're talking to a kid"* — simpler, and it re-answers
- *"Be more technical"*
- It remembers your last few questions, so you don't repeat yourself

**Music**
- *"Play me some music"* — picks something
- *"Play Bohemian Rhapsody by Queen"*
- *"Go to music.youtube.com and play me a music"*

**Open websites**
- *"Open YouTube"* · *"Go to facebook.com"* · *"Open Gmail"*
- Opens in **Brave**, in a separate window from your everyday one
- Sign into that window once and it stays signed in

**Meetings**
- *"What's my next meeting?"*
- *"Attend my meeting"* — joins muted, camera off
- *"What hardware are you using?"* — checks mic and camera first

**Remember your work** (for the meeting helper below)
- *"Note that I'm working on the batch layer"*
- *"Remember the audit slipped to November"*

**Control it**
- *"Start summarising"* / *"Stop summarising"*
- *"What model are you using?"* · *"Use the local model"*
- *"Hide"* · *"Shut up"*

---

## 3. Keyboard shortcuts

These always work, even if it mishears you.

| Key | Does |
|---|---|
| `Ctrl+Alt+S` | **Stop talking now** |
| `Ctrl+Alt+H` | Hide / show the overlay |
| `Ctrl+Alt+M` | Meeting (summary) mode on / off |
| `Ctrl+Alt+A` | Agent (mic, "Hey Nod") mode on / off |
| `Ctrl+Alt+Space` | Answer me *now* (in a meeting) |
| `Ctrl+Alt+C` | Let you click the overlay |
| `Ctrl+C` in the terminal | Quit |

---

## 4. Meeting helper

Nod can suggest answers when someone asks you something in a meeting.

**Set it up once:**

- Open `C:\Users\paulm\.nod\workcontext.md`
- Write a few lines about what you're working on
- Or just say: *"Hey Nod, note that I'm blocked on the vendor audit"*

**Use it:**

- Turn on meeting listening: `Ctrl+Alt+M` (first time takes ~10 seconds)
- When you're asked something, hit `Ctrl+Alt+Space`
- The overlay shows the question plus up to 3 answers you can say out loud

If it doesn't know, it says so instead of making something up.

---

## 5. Two modes

Nod has two halves. Turn them on and off independently.

| Mode | What it does | How |
|---|---|---|
| **Agent** | Listens to your mic for "Hey Nod" | `Ctrl+Alt+A`, or `--agent` at start |
| **Meeting** | Listens to your speakers, reads your screen, summarises | `Ctrl+Alt+M` |

They are independent — you can run either, both, or neither. To switch from one
to the other, press both keys.

The overlay's top line tells you which are on: `AGENT · MEETING`

**Tip:** leave meeting mode **off** unless you need it. It uses your daily
Google quota; agent mode alone is free.

---

## 6. If something goes wrong

| It says / does | Do this |
|---|---|
| Won't stop talking | `Ctrl+Alt+S` |
| Doesn't respond to "Hey Nod" | Check the overlay says `EARS: SAY 'HEY NOD'`; say it a bit slower |
| *"I couldn't reach the browser"* | Nothing — it reopens Brave next time |
| *"The local model isn't running"* | Open the Ollama app |
| *"I'm out of Gemini quota"* | Normal after heavy use. It keeps working locally, but can't look things up until tomorrow |
| Summaries stopped | Meeting mode is off — `Ctrl+Alt+M` |
| Overlay stuck on an old card | It clears itself after a minute |

**Two things worth knowing:**

- Speak **English** for commands. Tagalog works for questions, but commands
  like *"tama na"* or *"patugtog ka naman"* are only understood about half the
  time. Use the shortcuts instead.
- Nod only hears **your microphone** for commands. Anything playing through
  your speakers can never give it instructions.

---

## 7. Everyday commands

**Normal use — asking things, music, websites:**
```powershell
.\.venv\Scripts\python.exe -m copilot.main --agent --local-intent --no-summary --camera REDRAGON
```

**In a meeting — summaries and answer suggestions:**
```powershell
.\.venv\Scripts\python.exe -m copilot.main --agent --local-intent --camera REDRAGON --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

**Speaking too fast or wrong voice:**
```powershell
--voice Zira --speech-rate -2
```
