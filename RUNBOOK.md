# Nod — Runbook

Everything needed to get this running again from cold, assuming you remember
nothing. Written 2 August 2026.

Not meeting-only: it works on anything playing through your speakers — YouTube,
a lecture, a podcast, a stream — and tells you what the last two minutes were
about. That window is `ContextStore(window_seconds=120)` in `main.py`.

## Run it

Two lines, from `C:\paul\Nod`:

```powershell
.\.venv\Scripts\Activate.ps1
python -m copilot.main --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

If PowerShell refuses to run the activate script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

You can skip activation entirely by calling the venv interpreter directly —
this is what actually gets used and it needs no execution-policy change:

```powershell
& C:\paul\Nod\.venv\Scripts\python.exe -m copilot.main --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Stop it with `Ctrl+C` in the console, or close the console window. The overlay
is a tool window, so it is deliberately *not* in the taskbar or Alt+Tab.

## Agent mode — "hey Nod"

```powershell
python -m copilot.main --agent --camera REDRAGON --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Without `--agent` the microphone is never opened at all and Nod behaves exactly
as it did before.

Say **"hey Nod"** and it answers "Mhm?" / "Yes, boss?", then listens for 8
seconds. Or say it in one breath: **"hey Nod, what hardware are you using"**.

| Say | It does |
|---|---|
| any question | looks it up, answers in plain language |
| "open youtube" / "go to github.com" | opens it in Nod's Chrome |
| "note that I'm working on X" | saves it to your work context (see below) |
| "start / stop listening to my meeting" | switches meeting mode without a restart |
| "what mode are you in" | reports which halves are live |
| "use the local model" / "use gemini" | switches which brain answers |
| "what model are you using" | reports it |
| "explain it like you're talking to a kid" | re-answers the **last** question, simply |
| "play me some music" / "play Bohemian Rhapsody by Queen" | YouTube Music |
| "go to music.youtube.com and play me a music" | also YouTube Music — naming the site does not change that this is a music request |
| "what hardware are you using" | speaks and shows the mic/camera card |
| "attend my meeting" / "join my ten thirty" | finds the meeting, shows the card, joins muted |
| "what's my next meeting" | reads the next calendar entry |
| "hide" / "shut up" | hides the overlay |

### Interrupting it

Say **"hey Nod"** while it is talking and it stops. Long answers are spoken one
sentence at a time precisely so this works: SAPI's `Speak` blocks until the
whole string is done, so a five-sentence answer handed over in one piece is
five seconds during which nothing can stop it. Sentence by sentence, an
interruption takes effect at the next gap.

While Nod is speaking, **only the wake word is accepted** — anything else heard
then is most likely Nod's own voice in the microphone, and acting on that would
have it talking to itself.

### Explaining style

The default is plain language: everyday words, no jargon, short sentences.

| Say | Style |
|---|---|
| "explain like I'm five", "like a kid", "simpler" | `kid` |
| "plain English", "normally" | `plain` |
| "more technical", "in detail" | `technical` |

It **persists** until you change it, and changing it **re-answers the last
question** rather than just acknowledging — "explain it like a kid" is a request
for the explanation again, not a settings change.

The last six turns are kept, so follow-ups work without repeating yourself. Ask
how to fry an egg, then say "explain it like a kid", and it stays on eggs.

### Seeing what it heard

Every card the agent raises carries the transcript that caused it, right-
aligned on the eyebrow row in quotes:

```
AGENT                                        “play me ariana grande music”
Playing ariana grande
──────────────────────────────────────────────────────────────────────────
· ariana grande
· YouTube Music
```

Speech recognition is the least reliable link in the chain and the only one
you can correct, so when Nod acts on something it shows what it thought it was
told. This is most useful exactly when things go wrong — `Didn't catch that`
paired with `“koto facebook”` explains itself in a glance, where the same card
without the echo just looks broken for no reason.

It shares the eyebrow row rather than taking a line of its own, so the lead is
still the first thing the eye lands on, and it is elided rather than wrapped.
Summariser cards never show it: they are reporting on the room, not on an
instruction you gave.

The three cases that previously raised no card at all — `unknown`, a
classifier exception, and an empty classifier response — now do, precisely
because those are the ones where you most want the transcript.

### Step cards

When an answer is a procedure, the spoken reply stays short and the **steps go
on the overlay** rather than being read out — hearing a list and then seeing the
same list is just saying everything twice. Agent cards show up to six lines,
not the summariser's three, because a recipe truncated at three steps is not a
summary of the recipe.

## Switching modes without restarting

Nod has two halves and they are independently switchable at runtime:

| Half | What it does | Toggle |
|---|---|---|
| **Agent** | microphone, "hey Nod", commands | `--agent` at start |
| **Meeting** | speaker audio, transcription, screen OCR, summaries | `Ctrl+Alt+M`, or "hey Nod, start/stop listening to my meeting" |
| **Agent** | microphone, wake word, commands | `Ctrl+Alt+A` (no voice equivalent — see Hotkeys) |

The overlay's idle line shows which are live: `AGENT · MEETING`.

Every worker thread starts at boot and idles behind a flag rather than being
created on demand — a Python `Thread` cannot be restarted once `run()` returns,
so building them lazily would make "start listening" mean spawning a process.

The Whisper models are still loaded **lazily**, on the first toggle rather than
at startup. That keeps agent-only sessions at ~215 MB instead of ~490 MB, at
the cost of one ~10-second pause the first time you turn meeting mode on. After
that it is instant, because the model stays resident.

## Which model answers

| | Handled by | Uses quota |
|---|---|---|
| Understanding commands | local (`--local-intent`) or Gemini | local: none |
| Simple actions — open, play, hide, note, hardware | neither, just the classifier | none |
| Questions | Gemini, with tools | yes |

Say **"what model are you using"** to check, **"use the local model"** or
**"use gemini"** to force one for the session.

### When quota runs out

Nod no longer goes mute. It drops to the local model, says so **once**, and
keeps working — but with a hard limit it announces up front: *"I can't look
things up."*

That limit is deliberate. The local 3B model is precisely the thing that will
tell you the dollar-peso rate with total confidence and be two years out of
date — the failure `tools.py` exists to prevent. So in fallback it is
instructed to answer only from stable knowledge and to refuse anything
time-sensitive:

> **"what is the current usd to php exchange rate"** → *"I cannot check right now."*
> **"how many centimeters in an inch"** → *"One inch is equal to 2.54 centimeters."*

Cards are marked `answered locally, not looked up` so the provenance is never
ambiguous. Covered by the fallback test; if you loosen that prompt, re-run it.

## Answering your supervisor in a meeting

When someone puts a question to you mid-meeting, the overlay shows the question
plus up to three answers **you could say out loud verbatim** — not topics, not
advice about how to answer.

For that to be useful it has to know what you are actually working on, which no
transcript can tell it. That comes from:

```
C:\Users\paulm\.nod\workcontext.md
```

A plain markdown file. Edit it directly, or add to it by voice mid-meeting:

> **"hey Nod, note that the batch layer is blocked on the vendor audit"**

`Ctrl+Alt+Space` forces an immediate suggestion — that is the button to hit
when you have just been asked something and need an answer *now*, rather than
waiting for the next scheduled call.

### What it will and won't say

With work context, it answers from your notes:

> **Status of the batch layer?**
> · The ingestion code is finished and merged.
> · We are blocked waiting on the vendor SOC2 audit.
> · I'll check with Priya on the client communication.

With **no** relevant context it deliberately refuses to invent one, and offers
holding answers instead:

> · I'll check the current progress and follow up with you shortly.
> · Let me confirm the status with the team and get back to you today.

This is enforced in the prompt and covered by
`datasets/tests/test_answer_assist.py`, because the first version *did*
fabricate — it produced "on track for completion by the end of this sprint,
testing is underway, we expect no delays" from nothing at all. Those are
sentences you would have read off the screen and said to your manager. If you
edit the `reply` instructions in `llm.py`, run that test.

**Privacy:** the contents of `workcontext.md` are sent to Gemini with every
summariser call while the summariser is running. Keep it to what you would be
comfortable sending — and note it is *not* read at all in `--no-summary` mode.

Two lines to keep it short: the file is capped at 1,200 characters and the
**newest** notes win, so stale entries fall off the top on their own.

### It kept cutting me off

Fixed, and worth knowing why. Commands originally inherited the meeting
segmenter's settings: `HANG_BLOCKS = 7`, so **0.7 s of quiet ended your turn**,
and `STOP_RMS = 0.012`, which is high enough that a trailing "uhmmm" fell under
the bar and counted as silence.

Endpointing is now **adaptive**, because the two things being listened for want
opposite settings and one value has to be wrong for one of them:

| state | hang | why |
|---|---|---|
| idle (waiting for "hey Nod") | `HANG_IDLE = 6` (0.6 s) | the wake word takes 0.7 s and has no pause in it — waiting 1.6 s to be sure you finished saying it was the single biggest chunk of the delay before "Mhm?" |
| awake (you are dictating) | `HANG_AWAKE = 16` (1.6 s) | people stop to think inside a real instruction, and cutting them off costs the whole sentence |
| idle but already past 2.5 s | `HANG_AWAKE` | a one-breath "hey Nod, attend my meeting" switches to the patient setting mid-utterance |

Filler words are also recognised — "uh", "um", "hmm", "so", "well" — and hearing
only filler holds the window open instead of sending "um" off to be interpreted
as an instruction.

Tuning knobs are `HANG_IDLE` / `HANG_AWAKE` in `listen.py`. Change them and run
`python datasets\tests\test_endpointing.py`, which asserts the cut timing on
synthetic audio and takes a second.

### The command window is measured from when you start talking

`COMMAND_TIMEOUT` is how long Nod stays awake after saying "Yes?" — and what
it times is when you **start** the instruction, not when it finishes being
processed. That distinction was a bug worth understanding, because it produced
no error of any kind: Nod answered "Yes?", you said "play me some music", and
nothing happened at all.

The window used to be checked at dispatch, so everything in between spent it —
Nod speaking the acknowledgement, you reacting, the sentence itself, the 1.6 s
of silence `HANG_AWAKE` waits before believing the sentence ended, and whatever
lag the decode had accumulated. From `perf.log`:

```
607.281  ack.queued        Yes, boss?
614.031  utterance.closed  3.40s        <- 6.75 s of an 8 s window
```

That is a *short* command, and it left 1.25 s of headroom. Anything longer fell
off the end and was dropped in silence. `listen.py` now records
`_speech_started` on the segmenter's idle→active transition and judges the
window against that, which removes every term except your own reaction time.
`test_awake_window.py` pins it, including a case asserting that the old rule
would still fail — so raising `COMMAND_TIMEOUT` cannot quietly paper over a
regression in the rule itself.

If Nod ever ignores you again right after acknowledging, the status line now
says so: **`missed by 1.4s: play me some music`**. Silence there means the
utterance never reached the decoder at all, which is a microphone or
`START_RMS` problem, not a window problem.

### Interrupting, and Nod hearing itself

Because the listener now stays live while Nod speaks, it will hear Nod's own
voice through the mic. Two gates stop it acting on that, cheapest first:

1. **Before decoding** — while speaking, any utterance over 3 s is one of Nod's
   own sentences (the wake word is ~1 s), so it is dropped without running
   Whisper at all.
2. **After decoding** — if the text closely matches the sentence being spoken
   right now, it is echo and gets dropped.

The trade-off: a one-breath barge-in longer than 3 s while Nod is talking will
be ignored. Say a bare **"hey Nod"** to interrupt, then give the command.

A bare **"stop"** also works mid-answer, without the wake word. It has to:
requiring "hey Nod, stop" means saying six words over the top of the thing you
are trying to silence. The bar is narrow on purpose — the whole utterance must
be nothing but stop words (`STOP_WORDS` / `STOP_FILLER` in `listen.py`), under
2 s, and past the echo gate. "stop listening to my meeting" is a mode change
and still goes to the agent intact; `test_barge_in.py` holds that line.

Once stopped, Nod stays stopped. The `hide` intent now drains the speech queue
and cuts the sentence in progress instead of setting the HUD flag and then
speaking a confirmation over the silence it was just asked for.

### The name is heard as "none" more often than "Nod"

The single worst bug this project has had, and it hid for weeks because every
symptom pointed elsewhere: Nod answers once, then stops responding, then
answers again later. It reads as a hang, a rate limit, or a thread that died.

It was none of those. `perf.log` settles it — 18 attempts at the wake word in
one session, **7 matched**. The other 11 decoded as:

```
Hey, none.   Hey, Nun!   Hey, Nahn.   Hey Null.   Hey, nerd.   Hei na, ...
```

The final /d/ in "Nod" goes unreleased in ordinary speech, so Whisper hears a
nasal. Those score 0.57–0.75 against `nod` — nowhere near a threshold that
"nodded" does not also clear, so **no value of `NAME_RATIO` fixes it**. The
list was simply missing the renderings the microphone actually produces.

So names are now in two tiers:

| Tier | Matched | Why |
|---|---|---|
| `NAMES` | fuzzy, `NAME_RATIO` | `nod`, `naud`, `nawt` — not words anyone says by accident |
| `NAMES_WEAK` | **exact**, and guarded | `none`, `nun`, `null`, `nerd` — real English words |

The guard is `PROSE_NEXT`: a weak name followed by "of", "was", "because" and
friends is someone talking, not the wake word. It is what separates **"hey
none."** from **"hey, none of that matters now"**. Fuzzy-matching the weak tier
would wake on "Ned" and "kneed", which is why it is exact-only.

If Nod still misses your pronunciation, add the decode verbatim — read it off
`perf.log`, do not guess at the spelling — to `NAMES_WEAK` if it is an English
word and `NAMES` if it is not, then run `test_wake.py`.

### The rule that matters

The microphone and the speakers are **separate paths end to end** — separate
queues, separate transcribers, separate Whisper models. Only the mic can reach
the agent. This is deliberate and should stay that way: if they shared a queue,
a webinar host saying "hey Nod, join the next call" would be obeyed. A meeting
cannot give Nod orders, by construction rather than by filtering.

Relatedly, `speaker.ps1` takes text as **data on stdin** and never interpolates
it into a command. Nod's speech is assembled from calendar titles and speech
recognition, so a meeting called `$(Remove-Item ...)` gets read aloud instead of
run. Do not "simplify" that into a `-Command` string.

### Agent flags

| Flag | Default | Note |
|---|---|---|
| `--agent` | off | opens the mic and starts listening |
| `--local-intent` | off | classify commands on Ollama — no API quota |
| `--camera NAME` | first found | fragment, e.g. `REDRAGON` |
| `--mic-device NAME` | Windows default | currently Razer Barracuda X |
| `--wake-model` | `base.en` (English-only) | `base` for Taglish commands |
| `--wake-language` | `en` | `tl`, or `auto` to detect per utterance |
| `--ocr-active-window` | off | OCR the focused window, not the whole screen |

Command speech recognition is **English-only by default**, which is a reversal
of the earlier setting and is worth understanding before flipping it back.

The old reasoning was that commands are Taglish as often as English
("pakiplay naman ng music") and `.en` models do not degrade gracefully on
those. True. What it missed is that multilingual models do not degrade
gracefully either — given English audio with no context to anchor on, they
drift into whichever language the acoustics suit. All of these are English
spoken into the mic, decoded by `base`, taken from `perf.log`:

```
"go to youtube.com"  ->  "Kau tu youtube.com"
"go to Facebook"     ->  "Koto Facebook"
"hey Nod"            ->  "ให้นั้น?"        (Thai)
```

Pinning `--wake-language en` does not prevent it: the wake decode was *already*
pinned to English when it produced that Thai. Two syllables give language
detection nothing to work with, so the model has to be the English one.

The `Koto Facebook` case is the one that matters most, because it is not a
miss — it reached the classifier as a real instruction and was answered. A
wake word that fails is visible. A command that is confidently mistranscribed
is not.

Cost of the trade: genuine Taglish commands decode worse. `--wake-model base
--wake-language auto` takes it back the other way.

### Measuring latency

Nod's response time is spread across five threads and two processes, so "it
feels slow" is not a diagnosis. Turn on markers:

```powershell
$env:NOD_PERF = "1"
python -m copilot.main --agent --local-intent --no-summary --camera REDRAGON
# ...use it for a while, then:
python datasets\perf_report.py
```

Reports p50/p90 for each stage you actually wait through — wake→ack,
command→dispatch, brain round trip — plus how often self-hearing was suppressed.
Off unless `NOD_PERF=1`, and the log lands in `~/.nod/perf.log`.

### Asking it things

Any question routes to a tool-calling loop in `brain.py`. It is **not** RAG and
**not** MCP, and it is worth knowing why neither applies:

- RAG retrieves from a corpus you own. Nothing in your own documents contains
  today's exchange rate, so it cannot answer this class of question.
- MCP is a protocol for *serving* tools to a model, useful when the tools live
  in someone else's process. Here they live in `tools.py`, in the same repo.

What it does is plain tool calling: the model is told which functions exist,
picks one, Nod runs it, and the model answers from the result.

```
"hey Nod, what's the exchange rate for dollars to pesos"
   -> about 61 pesos to the dollar        (er-api, with a timestamp)
```

**Gemini's own `google_search` grounding does not work on this key.** It returns
429 `RESOURCE_EXHAUSTED` instantly, with no usage figure attached — that is an
entitlement refusal, not a limit that resets. So Nod does its own lookups.

Why this matters, measured: asked for the dollar-peso rate with no tool, Gemini
answered *"as of May 22, 2024, 1 USD = 58.35 PHP"*. Fluent, confident, two years
stale, about 5% wrong. The real answer was 61.25. A wrong number in a calm voice
is worse than "I don't know", because nothing in the delivery warns you.

| Tool | Source | Note |
|---|---|---|
| `exchange_rate` | open.er-api.com, frankfurter.app | exact, timestamped |
| `web_search` | Brave if keyed, else DuckDuckGo, else Wikipedia | see below |
| `fetch_page` | the page itself | when a snippet is too thin |

Static facts ("how many centimetres in an inch") are answered directly with no
lookup, which is correct — searching for those would just add a second of delay.

#### Quota — the thing that will actually stop it working

The free tier allows **500 requests per day, per model** — the quota is
`GenerateRequestsPerDayPerProjectPerModel`, so it is counted per model, not per
key. That detail is load-bearing.

The summariser fires every 8 to 40 seconds for as long as anything is playing.
At full tilt that is 450 requests an hour, so **an afternoon of meetings can
spend the whole day's allowance in about an hour** — and the symptom is not that
the HUD goes quiet, it is that the assistant stops answering.

Three things keep them apart:

| Lever | Where |
|---|---|
| Assistant on its own model (`gemini-3.5-flash-lite`) — its own 500/day | `brain.py`, `agent.py` |
| Summariser capped at 300/day, leaving 200 for the assistant | `llm.DailyBudget` |
| `--no-summary` skips the summariser entirely | `main.py` |

```powershell
# assistant only — costs almost nothing, no meeting summaries
python -m copilot.main --agent --no-summary --camera REDRAGON
```

The budget counter lives in `~/.nod/summary-budget.json` and resets by date.
Delete it to reset early. The daily quota itself resets on Google's clock, not
yours.

If you see **"I'm out of API quota for today"**, that is the 500 being gone,
not a bug. It comes back tomorrow.

Note that models differ in whether they accept `thinkingConfig.thinkingBudget`:
`3.1-flash-lite` requires it to stay fast, `3.5-flash-lite` returns 400 for it,
`3.5-flash` takes either. Both `brain.py` and `agent.py` retry once without it
rather than keeping a table that goes stale.

#### The search is the weak link

DuckDuckGo is scraped, not an API. It serves a steady trickle fine and blocks a
burst: about fifteen rapid queries earned a `202` "anomaly" page for everything
afterwards, and it cleared on its own in ~20 seconds. `tools.py` therefore
spaces requests out and retries once before giving up.

Critically, when search fails Nod reports **"search unavailable"** rather than
"no results" — given an empty result set the model treats the question as
answered and falls back on memory, which is the exact failure this is meant to
prevent.

If you want it reliable, Brave Search has a free tier, and signing up needs no
Google Cloud project — which matters given the block below:

```powershell
setx NOD_BRAVE_KEY "your-brave-key"     # then reopen the terminal
```

`tools.py` uses it automatically when set and falls back when it is not.

### Connecting your calendar — the OAuth path is blocked for you

Your work account is managed, and the org policy forbids creating Google Cloud
projects ("you do not have the permission to create a project in this
organization"). That is an admin setting; no code gets around it, and it should
not be worked around. Two honest options:

**Option A — the secret iCal address (no Cloud project needed):**

1. Google Calendar → hover your calendar → **⋮ → Settings and sharing**
2. Scroll to **Integrate calendar** → copy **Secret address in iCal format**
3. ```powershell
   setx NOD_ICS_URL "https://calendar.google.com/calendar/ical/..../basic.ics"
   ```

Nod uses it automatically when set. **Know the trade-off:** Google regenerates
that feed on its own schedule, so a meeting added or moved in the last hour may
not appear yet. Fine for "what's on this afternoon", not reliable for "join the
thing that just got moved". Your admin can also disable this feed entirely — if
the URL 404s, that is what happened.

**Option B — ask IT** for permission to create a Cloud project, or for a
project to be created for you with the Calendar API enabled. Then the OAuth
path below works and is real-time.

### The OAuth path, for reference (needs a Cloud project)

Your `GEMINI_API_KEY` does **not** grant calendar access; it is a different
kind of credential entirely. Calendar needs OAuth against your account:

1. <https://console.cloud.google.com/> → create a project (any name).
2. **APIs & Services → Library** → search "Google Calendar API" → **Enable**.
3. **APIs & Services → OAuth consent screen** → External → fill the three
   required fields → add **your own email** under *Test users*. Leaving it in
   testing mode is fine and avoids Google's verification review.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**.
5. **Download JSON**, and save it as:

   ```
   C:\Users\paulm\.nod\credentials.json
   ```

The first time you say "attend my meeting", a browser opens once to approve.
After that `~/.nod/token.json` is written and refreshes itself. Scope is
`calendar.readonly` — Nod can read your schedule and cannot change it.

**Neither file is in the repo**, and neither should ever be.

### Which browser Nod drives

**Brave by default**, then Chrome, then Edge. All three are Chromium, so the
DevTools protocol and every launch flag are identical — only the executable
differs. Force one with:

```powershell
setx NOD_BROWSER "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
```

It runs against its own profile at `~/.nod/browser`, **separate from your
everyday windows**. That is not a preference: Chromium only honours
`--remote-debugging-port` on the launch that *starts* a profile, so pointing it
at a profile you already have open silently hands off to the running window and
the port never opens. You sign into Nod's window once.

Note the profile is not portable between browsers — switching `NOD_BROWSER`
means signing in again.

### How joining works, and how it breaks

There is no API that makes *you* join a Meet. Nod drives a real Chrome through
the pre-join screen over the DevTools Protocol, turning the mic and camera off
**before** clicking join — joining first and muting second broadcasts whatever
your mic picked up in between.

That Chrome uses its own profile at `~/.nod/chrome`, not your everyday one.
This is not a preference: Chrome only honours `--remote-debugging-port` on the
launch that *starts* a profile, so pointing it at a profile you already have
open silently hands off to the running window and the port never opens. **You
must sign into that profile once**, the first time it opens.

This is the most fragile part of the project. It depends on Meet's DOM, which
Google reshuffles. Controls are located by `aria-label` rather than CSS class
because labels are accessibility surface and change far more slowly. When it
breaks, it says which step failed rather than pretending it worked — and if it
cannot confirm the microphone is off, **it refuses to join at all**.

## Running the classifier locally instead of on the API

`datasets/` exists to answer one question with numbers: **can a small model on
this machine classify Nod's commands as well as the API, and how much faster?**

```powershell
python datasets\make_commands.py                      # regenerate the data
python datasets\benchmark.py --backend gemini
python datasets\benchmark.py --backend ollama --model qwen2.5:3b-instruct
```

### What is and is not worth expecting

Three different things get called "training", and only one of them is available
on an RTX 3050:

- **Training from scratch** to Gemini-Flash's level: trillions of tokens,
  thousands of GPU-hours. Off by roughly six orders of magnitude. Not an option.
- **Fine-tuning** a 3B model with QLoRA: genuinely feasible here. But it teaches
  **form, not knowledge** — it changes how a model answers, it does not install
  facts. It will never learn today's exchange rate, and on small datasets it
  tends to *degrade* general ability.
- **Prompting** a small instruct model: no training at all, works today.

So a local model is worth it for **intent classification** — eight fixed
intents, structured output, on the latency critical path — and not for open
questions, where breadth of knowledge is the whole job. Live facts come from
`tools.py` regardless of which model is used.

### The data

`make_commands.py` generates 518 examples, split 392 train / 126 eval,
stratified so every intent appears in eval in proportion.

It generates them **dirty on purpose**, using the ASR errors seen in this
project's own transcripts: `meeting → meaning`, `ten thirty → 1030`, leading
`uhmmm` / `okay so`, and Taglish (`pakiplay naman ng music`). A model evaluated
only on tidy phrasing scores well and then meets a microphone.

**A mutation has to be an error the recogniser actually makes.** An earlier
version also had `play → pray`, `music → musik` and `explain → explaine`, which
were invented rather than observed. They produced rows like `"pray taylor
swift"` — not a music request in any language — and cost real accuracy while
teaching nothing. Adding noise is not the same as adding realism; if you extend
`ASR_SLIPS`, extend it from a transcript, not from imagination.

The first version produced 243 rows instead of 600 because randomised mutations
collided and deduplicated — and what deduplicated away was disproportionately
the *messy* cases, the ones that matter. Each variant now applies one specific
kind of damage, which is why the counts are deterministic.

`SEED` is fixed. An eval set that changes between runs is not an eval set.

### Results

`qwen2.5:3b-instruct` on an RTX 3050. The eval set grew from 126 to 155 rows
when `open_site` and `note` were added, so the two runs are not directly
comparable — the 10-intent task is harder than the 8-intent one.

**8 intents, 126 examples (3 Aug 2026):**

| setup | accuracy | p50 | malformed |
|---|---|---|---|
| zero-shot | 87.3% | 581 ms | 0 |
| **16 worked examples** | **98.4%** | **606 ms** | 0 |

**10 intents, after `open_site` + `note` were added:**

| dataset | eval rows | 2 shots | 3 shots | p50 |
|---|---|---|---|---|
| with invented ASR slips | 155 | 95.5% | 94.8% | 578 ms |
| **invented slips removed** | 148 | **99.3%** | 98.0% | 573 ms |

Measured at both shot counts on purpose: the few-shot examples are drawn from
the train split in file order, so changing the dataset also changes which
examples land in the prompt. If the gain only showed at one shot count it would
be a lucky draw rather than cleaner data. It shows at both.

Two shots is the production setting (`local_intent.SHOTS_PER_INTENT`) — better
*and* a smaller prompt.

The 3.8-point jump came from deleting seven bad rows, not from tuning anything.
Inspecting the failures showed inputs like `"pray taylor swift"` — produced by a
`play → pray` substitution that had been asserted, not observed. Those rows are
unanswerable by construction: no correct classifier maps them to music, because
they are not music requests. They were dragging the score down while teaching
nothing.

Two caveats on that number, so it is not read as better than it is:

- The eval set changed (155 → 148 rows), so it is **not** directly comparable
  to 95.5%. Removing impossible items makes an eval more valid, not easier —
  but it is still a different measurement.
- More shots did **not** help on the old data (2-shot 95.5%, 3-shot 94.8%),
  which is what pointed at the dataset rather than the prompt.

Notably **zero `play_music` ↔ `open_site` confusions** — "go to
music.youtube.com and play me a music" reliably reads as a music request rather
than a request to open a website, which is the boundary those two intents were
most likely to blur.

The one remaining failure is a Tagalog `hide` phrasing. English "hide" / "shut
up" work, and `Ctrl+Alt+H` always works, so this is a known and bounded gap
rather than something to tune the dataset around.

### When adding an intent, check what it collides with

Adding `mode` and `model` dropped accuracy from 99.3% to **91.9%**, and the
failure table said exactly why:

```
hide           → mode            3
attend_meeting → mode            2
mode           → attend_meeting  2
```

Not model weakness — a naming collision I introduced. "Start listening to my
**meeting**" (turn on transcription) sits almost on top of "attend my
**meeting**" (join a Google Meet call): same words, one joins a call other
people can see, the other just starts paying attention to audio already
playing. Likewise "stop listening" against `hide`'s "stop talking".

Two fixes, both about being explicit rather than hoping the model infers:

1. The `SYSTEM` prompt in `agent.py` now contrasts the three head-on —
   *get into a call* vs *start paying attention* vs *stop talking* — instead of
   describing each in isolation.
2. The seed lists lead with unambiguous phrasings ("start summarising", "watch
   my screen", "stop taking notes") so the boundary is learnable from examples,
   with the genuinely ambiguous ones kept but outnumbered.

The lesson generalises: when you add an intent, look at what it takes words
from, and read the confusion table before assuming the model is at fault.

### Where accuracy actually sits, and why it stops there

After disambiguation the 12-intent set lands around **95%**, not the 99.3% the
10-intent set reached. That gap is mostly not fixable, and it is worth writing
down why rather than rediscovering it:

| Remaining failures | Share | Fixable? |
|---|---|---|
| Tagalog imperatives — `patugtog ka naman`, `tama na`, `tumahimik ka` | ~60% | Not with a 3B model |
| Genuinely ambiguous — a bare `stop` is "stop talking" *or* "stop listening" | ~20% | No correct label exists |
| Borderline labels of my own making | ~20% | Yes, and were |

Two things could be done and were deliberately **not**:

- **Deleting the Tagalog rows** would put the number above 97% immediately. It
  would also be a lie about what Nod does — those are real phrasings a real
  user says, and a benchmark that excludes the hard half measures nothing.
- **Relabelling ambiguous rows to match the model** is tuning to the test.

The practical mitigation is that the two commands most likely to be misheard
have hotkeys that do not involve the classifier at all: `Ctrl+Alt+H` to hide
and `Ctrl+Alt+M` for meeting mode. English phrasings work reliably; if you
routinely give commands in Tagalog, a larger local model (`qwen2.5:7b`, ~4.5 GB,
still fits this GPU) is the lever — not more prompt engineering.

**The gap between those two rows is the finding.** Zero-shot, nine of sixteen
errors were the `style` intent — "explain like I'm five" landing as `hide`,
`unknown` or `ask`. Sixteen examples in the prompt removed every one of them,
for about 25 ms.

That matters because the obvious next move — fine-tuning — would have cost
hours and produced something harder to change: adding a ninth intent later
would mean retraining, where now it means adding two lines to the dataset.
**Fine-tuning is what you do when prompting has been tried and is not enough.**
It had not been tried.

Zero malformed JSON in 252 calls also settles the other worry about small
models: schema-following was a non-issue.

To use it:

```powershell
ollama pull qwen2.5:3b-instruct
python -m copilot.main --agent --local-intent --camera REDRAGON
```

Only *classification* moves locally. Answers still come from `brain.py` on the
API, because open questions need breadth a 3B model does not have. If Ollama is
not running, it falls back to the API rather than going deaf.

The model occupies ~2.2 GB of VRAM, leaving ~4 GB free on this card. First load
takes ~60 s.

It is pinned resident with `keep_alive: -1`. Ollama evicts an idle model after
five minutes by default, which meant the first command after a quiet spell paid
a multi-second reload — and that read as "Nod is randomly slow", not as anything
to do with idling. The cost is ~2.2 GB of VRAM held continuously.

### Test suites

No pytest — these are plain scripts with exit codes, matching the rest of the
repo:

```powershell
python datasets\tests\test_wake.py            # 45 cases, both directions
python datasets\tests\test_endpointing.py     # synthetic audio, cut timing
python datasets\tests\test_filler.py          # "uhmmm" must not dispatch
python datasets\tests\test_barge_in.py        # "stop" stops; browser != request
python datasets\tests\test_awake_window.py    # the command after "Yes?" lands
python datasets\tests\test_answer_assist.py   # needs GEMINI_API_KEY, 2 requests
python datasets\tests\test_spoken_errors.py   # failures must be sayable
python datasets\tests\test_startup.py         # every bus attribute exists
```

`test_spoken_errors.py` asserts mechanically — no URLs, no ports, no exception
class names, nothing over 80 characters — because Nod once read a full
`ConnectionError` aloud, host and retry count included, and it took fifteen
seconds. The first case in that file is that exact string.

`test_wake.py` exists because fuzzy matching is also how you wake on "he nodded
and walked away"; the negative cases are the ones that regress quietly. Its
`SHOULD_WAKE_FIELD` block is different in kind from the rest of the suite —
every string in it is a verbatim decode from `perf.log` taken while the user
said "hey Nod" and Nod sat there. They are not guesses about how Whisper might
mishear the name. They are how it did.
`test_answer_assist.py` exists because the first version of that prompt
fabricated a project status out of nothing.

### Reading the benchmark

Malformed JSON is counted separately from wrong answers, because they are
different problems with different fixes — a model that understands the task but
cannot hold a schema wants grammar-constrained decoding or fine-tuning, while a
model picking the wrong intent wants better prompting or a bigger model.

## Hotkeys

| Key | Does |
|---|---|
| `Ctrl+Alt+H` | hide / show the overlay |
| `Ctrl+Alt+Space` | force an immediate Gemini call |
| `Ctrl+Alt+C` | toggle click-through (see below) |
| `Ctrl+Alt+S` | **stop talking now** |
| `Ctrl+Alt+M` | meeting (summary) mode on / off |
| `Ctrl+Alt+A` | agent (mic, "hey Nod") mode on / off |

`Ctrl+Alt+M` and `Ctrl+Alt+A` are the two halves of Nod, and they are
**independent, not exclusive** — both on is the normal way to run in a meeting,
and both off leaves the overlay up and idle. To switch from one to the other,
press both. There is no single cycling key, because three of the four states
are useful.

`Ctrl+Alt+A` is safe to leave off and turn on later: every worker starts at
boot and idles behind its flag, so the first enable pays the wake model's load
(~1 s, cached) and later ones are instant. It has no voice equivalent on
purpose — "hey Nod, stop listening to me" is a one-way door, since the thing
that would hear you turn it back on is the thing being turned off.

`Ctrl+Alt+S` exists because saying "hey Nod" was previously the only way to cut
a long answer short, and talking over something to make it stop talking is the
wrong shape for "be quiet" — particularly in a meeting.

The overlay is click-through by default — clicks land on the window behind it.
`Ctrl+Alt+C` turns that off if you ever need to interact with it directly.

## Environment

Both are already set permanently via `setx`, so a new terminal picks them up.
They are **not** in any file in this repo.

| Variable | Value | Why |
|---|---|---|
| `GEMINI_API_KEY` | *(your key)* | read by `copilot/llm.py` |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | **required** — see below |

To confirm they survived a reboot: `echo $env:GEMINI_MODEL`

To change the key later:

```powershell
setx GEMINI_API_KEY "new-key-here"     # then open a NEW terminal
```

### Why GEMINI_MODEL has to be set

`llm.py` defaults to `gemini-2.5-flash-lite`, which Google has since closed to
new API keys — it returns:

```
404  This model is no longer available to new users.
```

`gemini-3.1-flash-lite` is the current equivalent and works with the file
unchanged, at ~1.1 s per call, which fits the latency budget in the README.
`llm.py` reads `GEMINI_MODEL` from the environment, so this is a config
override, not a code edit.

Two nearby models that do **not** work: `gemini-3.5-flash-lite` and
`gemini-flash-lite-latest` both return HTTP 400, because that generation no
longer accepts `thinkingConfig: {thinkingBudget: 0}` — the line in `llm.py`
that keeps latency down. If you ever move to those, that config has to change
to `thinking_level` first.

To see what your key can currently reach:

```powershell
& C:\paul\Nod\.venv\Scripts\python.exe -c "import os,requests;print('\n'.join(m['name'] for m in requests.get('https://generativelanguage.googleapis.com/v1beta/models',headers={'x-goog-api-key':os.environ['GEMINI_API_KEY']}).json()['models']))"
```

## Paths worth knowing

| Thing | Where |
|---|---|
| Tesseract binary | `C:\Program Files\Tesseract-OCR\tesseract.exe` |
| Whisper model cache | `C:\Users\paulm\.cache\huggingface\hub\` |
| Virtualenv | `C:\paul\Nod\.venv` (Python 3.10) |

Three Whisper models are cached, all downloaded already:

| Folder under `models--Systran--faster-whisper-…` | Size | Note |
|---|---|---|
| `-small` | ~460 MB | multilingual — use for Tagalog meeting audio |
| `-small.en` | 464 MB | **the meeting default**, English-only — see below |
| `-base` | ~140 MB | multilingual, faster and rougher |
| `-base.en` | ~140 MB | **the wake-word default**, English-only |

Tesseract is **not on PATH** — that is why `--tesseract` is passed explicitly.
Without it, OCR fails silently and the HUD just never gets screen text.

Delete a Whisper cache folder and it re-downloads (~500 MB, several minutes,
looks frozen while it happens). It does not need re-downloading otherwise.

### Language — read this before changing `--model`

**The default is now `--model small.en --language en`, and this is the one
setting in Nod that can be wrong without looking wrong.** It was flipped on
request, for a pure-English setup. If you feed it Tagalog, revert it.

The measurement behind the old default still stands and has not been redone.
On a 30 s clip of *Filipino* audio:

| model | speed | result |
|---|---|---|
| `small.en` | **1.03x realtime** — cannot keep up | gibberish |
| `base` multilingual | 0.11x | coherent but mangled |
| `small` multilingual | **0.33x** — 3x headroom | accurate Taglish |

`small.en` was slow *because* it was hallucinating repetition loops on
non-English audio — it emitted confident English nonsense ("BAHAN!", "Never
gonna", "grade 7 student") which was then summarised as though it were real.

So the two paths now carry opposite risks, because they listen to different
things:

| Path | Source | Default | Fails on |
|---|---|---|---|
| mic — wake word, commands | **you**, speaking English | `base.en` | your Taglish commands |
| speakers — meeting, summaries | **whatever is playing** | `small.en` | Tagalog meeting audio |

The mic side is settled: it is your own voice, it is English, and `perf.log`
shows the multilingual model mangling it. The speaker side is a judgement about
what you will be listening to, and only you know that. If a meeting turns out
to be Filipino, the summaries will be confident fiction rather than obviously
broken, so switch back for it:

```powershell
# meeting audio is Tagalog/Taglish — mic stays English-only
python -m copilot.main --agent --model small --language auto
```

`--language auto` restores per-utterance detection, which handles Taglish
code-switching. `--language tl` pins Filipino, which is faster and steadier
when you know the whole session is in it.

### Your GPU is not being used, deliberately

You have an RTX 3050 (8 GB), but `faster-whisper` on CUDA fails here with
`Library cublas64_12.dll is not found`. Enabling it means pip-installing
`nvidia-cublas-cu12` and `nvidia-cudnn-cu12` (~700 MB). It was **not** done,
because CPU already runs at 0.33x realtime. If you ever want it:

```powershell
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
python -m copilot.main --device cuda --compute-type float16 ...
```

## Audio

Capture is WASAPI loopback on the **default speaker** — whatever Windows is
playing through. No virtual audio device needed.

On this machine the devices are:

```
Realtek Digital Output (Realtek(R) Audio)
Speakers (Razer Barracuda X 2.4)               <- current default (headset)
ATLAS HD 236C (NVIDIA High Definition Audio)
```

Windows switches the default automatically when the Razer headset connects, and
Nod follows it — no flag needed. **But the device is bound at startup**, so if
you connect or disconnect the headset while Nod is running, restart it.

**If the HUD stays quiet, this is the first thing to check.** It captures the
Windows *default* output. If you switch playback to the Razer headset but the
default is still the NVIDIA HDMI output, it will hear silence. Either change
the Windows default, or name the device:

```powershell
python -m copilot.main --audio-device "Speakers (Razer Barracuda X 2.4)" --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

List devices any time:

```powershell
python -m copilot.main --list-devices
```

## Flags

| Flag | Default | Use when |
|---|---|---|
| `--tesseract PATH` | none | **always, on this machine** |
| `--audio-device NAME` | default speaker | playback is not on the default device |
| `--language CODE` | auto-detect | `tl` or `en` to pin it and skip detection |
| `--model` | `small.en` (English-only) | `small --language auto` for Tagalog, `base.en` if it lags |
| `--device` / `--compute-type` | `cpu` / `int8` | `cuda` / `float16` if you want the GPU |
| `--ocr-interval` | `1.0` | raise it if CPU is pegged |
| `--no-vision` | off | disables screenshots + OCR entirely |
| `--monitor` | `1` | pick a different display |
| `--list-devices` | — | print audio devices and exit |

## Performance work — what changed and why

A pass over the whole system, after the feature work. The measurements are
recorded because several of them contradicted the obvious guess.

| Change | Where | Effect |
|---|---|---|
| Adaptive endpointing | `listen.py` | ~1.0 s off every "hey Nod" — the largest single win |
| Wake ack no longer blocks | `listen.py`, `voice.py` | listener keeps running instead of stalling ~1 s on SAPI |
| Echo gates before/after decode | `listen.py` | Nod no longer Whisper-decodes its own voice |
| `requests.Session` reuse | `brain.py`, `tools.py` | removes a TLS handshake per call; a 2-round answer made 4+ |
| Ollama `keep_alive: -1` | `local_intent.py` | kills the multi-second reload after 5 min idle |
| Hardware detect cached 60 s | `hardware.py` | a PowerShell spawn per device command, gone |
| OCR skipped in `--no-summary` | `main.py` | it was running Tesseract every second into a store nothing read |
| Shared Gemini post helper | `llm_util.py` | the summariser had no thinkingConfig-400 retry; now all three callers share one |
| Retired model default | `llm.py` | `gemini-2.5-flash-lite` 404s for new keys — a fresh install looked broken |

**One optimisation was measured and reverted.** Skipping the 1.5× upscale in
`vision._prep` on 1080p captures made OCR 1.6× faster and looked obviously
correct — the frame is already large. On the same frame it retained **13% of
the tokens** and found 2,556 characters against 3,548. The upscale is not about
the size of the image, it is about the size of the *text*: UI and slide text
sits at 12–14 px whatever the screen resolution, and Tesseract needs it scaled
to ~20 px. The reasoning is now a comment in `_prep` so it does not get
"optimised" again.

## What was changed from the downloaded files

Three things, all still in the original design — noted here so they are not a
mystery in two weeks:

1. **`copilot/transcribe.py` was written from scratch.** It was missing from
   the download; `main.py` imports `Transcriber` from it. It follows the
   contract the other modules already fixed: drain `bus.audio`, feed blocks to
   the existing `Segmenter` in `audio.py`, decode with faster-whisper, push
   `TranscriptSegment` onto `bus.transcripts`.
2. **Hotkey string fix in `main.py`.** It read `<ctrl>+<alt>+space`; pynput
   requires `<space>`, and the bare form raised `ValueError: space` at startup,
   which killed the whole app before the overlay appeared.
3. **Hotkeys now cross threads via Qt signals.** They previously called
   `hud.setVisible()` straight from pynput's listener thread, which breaks
   Qt's rule that widgets are touched only from the main thread — the rule
   `main.py`'s own docstring states. `HUD` now exposes `toggle_visible` and
   `request_click_through` signals; Qt queues them onto the main thread.

Then, making it work beyond meetings:

4. **The prompt in `llm.py` was rewritten** to cover any live audio — video,
   lecture, podcast, stream — defaulting to "what were the last two minutes
   about". It also now states that **the transcript beats the screen text**.
   That mattered: screen OCR runs at roughly 4x the volume of the transcript
   (1800 chars vs ~430), so without it the overlay kept summarising whatever
   was on screen instead of what you were listening to.
5. **The HUD is masked out of its own screenshot** (`vision.py::_mask_hud`).
   This was the worst bug found. The overlay is always-on-top, so a full-screen
   grab contains it; OCR read the overlay's own last suggestion back in as
   fresh context, and it kept confirming itself in a loop. `HUD._relayout()`
   now publishes its rectangle to `bus.hud_rect` as a plain tuple — no Qt
   object crosses a thread boundary — and the screen reader blanks that region
   before diffing and OCR.
6. **`Segmenter.MAX_UTTERANCE` 12 s → 8 s.** Silence-cutting assumes a quiet
   room. On a 45 s clip of your actual audio there was exactly **one** silence
   gap long enough to close an utterance, so in practice every cut comes from
   this timeout. It is the real latency knob for anything but a meeting.
7. **`--model` default `small.en` → `small`, and `--language` added.** See the
   Language section above. *Later reversed back to `small.en` / `--language en`
   on request, for an English-only setup; the Tagalog measurement that
   motivated this entry still stands and is documented there.*
8. **Lead length 90 → 75 characters.** Measured against the overlay's actual
   font and width: it fits ~78 characters, so 90 meant leads were silently
   truncated with an ellipsis and the last words lost.

Nothing else was modified. The threading model is unchanged, nothing was
Dockerized, and `docs/BATCH-LAYER-BRIEF.md` and `docs/Nod-Motion-Deck.html` are
parked there unused.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Overlay never appears | check the console for a traceback; it exits rather than hiding errors |
| `QWindowsContext: OleInitialize() failed` `COM error 0x80010106` | cosmetic. `soundcard` claims the main thread for COM's multi-threaded apartment at import; Qt then wants the single-threaded one and logs this. `main.py` now claims STA before any import, which reverses who loses — soundcard tolerates that, Qt does not. If it returns, something new is importing a COM library above that block |
| Nod acknowledges, then ignores the command | the awake window — see above. The status line says `missed by Ns:` when that is what happened |
| Overlay shows `LISTENING` forever | no audio on the default device — see Audio above |
| HUD goes quiet mid-session | rate limit; `llm.py` backs off 60 s after a 429. Lower `RPM_LIMIT` |
| `gemini: GEMINI_API_KEY not set` | new terminal did not pick up `setx` — reopen it |
| `404 no longer available` | `GEMINI_MODEL` is unset, so it fell back to the retired 2.5 model |
| OCR never contributes | `--tesseract` not passed, or you're in `--no-summary` (OCR is skipped there by design — nothing reads it) |
| Answers ignore your work | `~/.nod/workcontext.md` empty, or meeting mode is off |
| "I couldn't reach the browser" | you closed Nod's Chrome — it relaunches on the next command, no restart needed |
| "The local model isn't running" | Ollama stopped; `ollama serve` or reopen the app |
| Nod won't stop talking | say "stop", or `Ctrl+Alt+S` |
| "hey Nod" ignored, then works, then ignored | almost always the name being decoded as something not in `NAMES` / `NAMES_WEAK`. Run with `NOD_PERF=1` and read the `decode.done` lines in `~/.nod/perf.log` — the answer is written there verbatim. It is not a rate limit: nothing throttles the wake word |
| Wake word ignored **only** while Nod talks | expected for anything over 3 s — that is the echo gate. A bare "hey Nod" or "stop" gets through |
| Summaries stopped | meeting mode off — `Ctrl+Alt+M`, check the overlay's idle line |
| Long silence at startup | normal on first run only (model download); ~10 s warm |
| Transcript is fluent nonsense | English-only model on non-English audio — use `--model small` |
| It summarises your screen, not the audio | speech too sparse to outweigh OCR; try `--no-vision` |
| It keeps repeating its own last line | the HUD mask regressed — check `bus.hud_rect` is being set |
| Text lags badly behind speech | decode slower than realtime; `--model base`, or enable CUDA |
