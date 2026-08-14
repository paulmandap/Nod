"""Build the labelled command dataset used to evaluate (and optionally train)
a local intent classifier.

Written as a generator rather than a hand-typed file so the mutations can be
applied systematically: the point of this data is not clean phrasing, it is
what Whisper actually hands over. Observed in this project's own transcripts:

  * "attend my meeting" came through as "attend my meaning"
  * "ten thirty" became "1030" after normalisation stripped the colon
  * filler survives into the text -- "uhmmm", "so", "okay" lead a lot of turns
  * Taglish is normal here, so "pakiplay naman ng music" has to classify

A model that only ever sees tidy input scores well on tidy input and then meets
a microphone. Roughly a third of what comes out of here is deliberately messy.

Output is JSONL, one example per line:

    {"text": ..., "intent": ..., "style": ..., "query": ..., "when": ...}

Usage:  python datasets/make_commands.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20260803          # fixed: an eval set that changes is not an eval set
OUT = Path(__file__).parent

# --- seed phrasings -----------------------------------------------------------

ATTEND = [
    "attend my meeting", "join my meeting", "join my next call",
    "get me into the standup", "hop into my {time}", "join the {name}",
    "can you join my meeting", "take my meeting", "get me in the call",
    "join my {time} meeting", "attend the {name} for me",
    "put me in the meeting", "join the call for me", "sit in on my meeting",
    "i have a meeting can you join", "join my next meeting please",
    "sali ka sa meeting ko", "pasok ka sa meeting",
]
NEXT = [
    "what's my next meeting", "what's on my calendar", "do i have any meetings",
    "what's coming up", "when is my next call", "am i free right now",
    "what's my schedule today", "anything on my calendar this afternoon",
    "do i have anything after lunch", "what meeting is next",
    "ano ang next meeting ko", "may meeting ba ako mamaya",
]
HARDWARE = [
    "what hardware are you using", "which microphone will you use",
    "what mic are you on", "which camera do you use", "what devices are you using",
    "are you using my headset", "what's my microphone set to",
    "check my devices", "which speakers are you on", "what camera is set up",
    "anong mic ang gamit mo",
]
HIDE = [
    "hide", "hide yourself", "go away", "shut up", "be quiet", "stop talking",
    "dismiss", "hide the overlay", "get off my screen", "nevermind",
    "stop", "quiet please", "tumahimik ka", "itago mo sarili mo",
    # Added after `hide` lost three examples to `mode`: these are the bare,
    # ambiguous forms people actually use when they want silence, and without
    # them the boundary against "stop listening" was learned from too few cases.
    "stop it", "enough", "that's enough", "quiet", "shush", "cancel that",
    "stop speaking", "hide now", "tama na", "wag ka na magsalita",
]
MUSIC = [
    ("play me some music", ""),
    ("play music", ""),
    ("put some music on", ""),
    ("play something", ""),
    ("i want to listen to music", ""),
    ("play me a song", ""),
    ("patugtog ka naman", ""),
    ("pakiplay naman ng music", ""),
    ("play bohemian rhapsody by queen", "bohemian rhapsody queen"),
    ("play shape of you by ed sheeran", "shape of you ed sheeran"),
    ("can you play hotel california", "hotel california"),
    ("play some lofi", "lofi"),
    ("put on some jazz", "jazz"),
    ("play taylor swift", "taylor swift"),
    ("play the beatles", "the beatles"),
    ("play some opm", "opm"),
    ("play imagine by john lennon", "imagine john lennon"),
    ("play me some classical music", "classical"),
    ("play up dharma down", "up dharma down"),
    ("i want to hear ben and ben", "ben and ben"),
    # Naming the site as the *means* of playing. These stay play_music: the
    # request is for music, and the website is incidental. Without them the
    # classifier has no way to learn where the boundary with open_site sits,
    # and "go to music.youtube.com and play me a music" is exactly how people
    # actually ask.
    ("please go to music.youtube.com and play me a music", ""),
    ("go to music.youtube.com and play something", ""),
    ("open youtube music and play me a song", ""),
    ("go to youtube music and play ben and ben", "ben and ben"),
    ("open music.youtube.com and play some jazz", "jazz"),
    ("go on youtube and play bohemian rhapsody", "bohemian rhapsody"),
    ("pumunta ka sa youtube music at magpatugtog", ""),
]

# Opening a site with no other action attached. The discriminator against
# play_music is the absence of a play verb, which is why several of these are
# deliberately near-misses of the music rows above.
OPEN = [
    ("open youtube", "youtube"),
    ("go to youtube", "youtube"),
    ("open github.com", "github.com"),
    ("go to github.com", "github.com"),
    ("pull up my email", "email"),
    ("open gmail", "gmail"),
    ("open my calendar", "calendar"),
    ("go to google", "google"),
    ("open google drive", "drive"),
    ("take me to news.ycombinator.com", "news.ycombinator.com"),
    ("open youtube music", "youtube music"),
    ("buksan mo ang youtube", "youtube"),
    ("pakibuksan ang gmail", "gmail"),
]

# Turning meeting listening on and off at runtime, or asking what is on.
# Deliberately weighted towards phrasings that cannot be confused with
# attend_meeting ("join the call") or hide ("be quiet"). The first measurement
# after adding this intent lost 7 points to exactly those two collisions:
# "start listening to my meeting" vs "attend my meeting", and "stop listening"
# vs "stop talking". The overlapping phrasings are kept -- people really do say
# them -- but they are outnumbered by unambiguous ones so the boundary is
# learnable from the examples rather than only from the prompt.
MODE = [
    ("start summarising", "on"),
    ("start summarising my meeting", "on"),
    ("start transcribing", "on"),
    ("watch my screen", "on"),
    ("read my screen", "on"),
    ("start taking notes", "on"),
    ("start meeting mode", "on"),
    ("turn on meeting mode", "on"),
    ("start listening to my meeting", "on"),
    ("listen to what's playing", "on"),
    ("simulan mo ang pagsusummarize", "on"),
    ("stop summarising", "off"),
    ("stop transcribing", "off"),
    ("stop watching my screen", "off"),
    ("stop taking notes", "off"),
    ("stop meeting mode", "off"),
    ("turn off meeting mode", "off"),
    ("stop listening to my meeting", "off"),
    ("itigil mo ang pagsusummarize", "off"),
    ("what mode are you in", ""),
    ("are you summarising right now", ""),
    ("are you listening to my meeting", ""),
    # "what are you doing right now" was here and has been removed: it is a
    # question in plain English, and classifying it as `mode` rather than `ask`
    # is a defensible reading at best. Grading the model down for choosing the
    # other one measures the label, not the model.
]

# Which brain answers questions.
MODEL = [
    ("what model are you using", ""),
    ("which model is this", ""),
    ("are you using gemini", ""),
    ("use the local model", "local"),
    ("switch to local", "local"),
    ("use ollama", "local"),
    ("answer locally from now on", "local"),
    ("use gemini", "gemini"),
    ("switch back to gemini", "gemini"),
    ("go back to the cloud model", "gemini"),
]

# Facts about the user's own work, saved for meeting answer-assist.
NOTE = [
    ("note that i'm working on the batch layer", "working on the batch layer"),
    ("remember that the soc2 audit slipped to november",
     "the soc2 audit slipped to november"),
    ("make a note that i finished the overlay masking",
     "i finished the overlay masking"),
    ("note i'm blocked on the vendor review", "blocked on the vendor review"),
    ("remember i'm on leave next friday", "on leave next friday"),
    ("take note that the demo is on thursday", "the demo is on thursday"),
    ("note that priya owns the migration", "priya owns the migration"),
    ("tandaan mo na tapos na ang latency fix", "tapos na ang latency fix"),
]
STYLE = [
    ("explain it like i'm five", "kid"),
    ("explain like you're talking to a kid", "kid"),
    ("can you explain that more simply", "kid"),
    ("say that in simpler terms", "kid"),
    ("dumb it down for me", "kid"),
    ("explain that like i'm a child", "kid"),
    ("make it simpler", "kid"),
    ("paliwanag mo parang bata", "kid"),
    ("explain that normally", "plain"),
    ("go back to plain english", "plain"),
    ("just say it normally", "plain"),
    ("be more technical", "technical"),
    ("give me the technical version", "technical"),
    ("explain that in detail", "technical"),
    ("i want the proper explanation", "technical"),
]
ASK = [
    "what's the exchange rate for dollars to pesos", "how do i fry an egg",
    "what's the latest iphone", "who is the president of the philippines",
    "what's the weather like", "how tall is mount everest",
    "what time is it in london", "how do i boil rice",
    "what's the capital of japan", "how many centimeters in an inch",
    "who won the game last night", "what's the population of manila",
    "how do i change a tire", "why is the sky blue",
    "what does api stand for", "how do i make coffee",
    "tell me a joke", "what's a good recipe for adobo",
    "how far is the moon", "what is machine learning",
    "explain how wifi works", "what's the best way to learn python",
    "how do i tie a tie", "what causes rain",
    "who wrote romeo and juliet", "how much is bitcoin right now",
    "what's the tallest building in the world", "how do i clean a cast iron pan",
    "what's the score in the nba game", "how do you say thank you in japanese",
    "ano ang kabisera ng pilipinas", "paano magluto ng sinigang",
    "what's the traffic like on edsa", "how do i restart my router",
    "what's the difference between ram and storage",
]
UNKNOWN = [
    "asdf jkl qwerty", "xkcd zzz", "mmm hmm ahh", "brrr",
    "aaaa", "sdfsdf", "uh huh mm", "zzz zzz zzz",
    "grrrmph", "ba ba ba ba", "tsk", "hmmmmmmm",
]

TIMES = ["ten thirty", "1030", "nine am", "two o'clock", "three thirty", "1 pm"]
NAMES = ["standup", "sprint planning", "retro", "sync", "one on one",
         "all hands", "design review", "daily"]

# --- mutations that mimic the microphone -------------------------------------

FILLERS = ["uh", "um", "uhmmm", "so", "okay so", "well", "hmm", "ah"]

# Substitutions Whisper genuinely makes on this vocabulary -- observed in this
# project's own transcripts, or orthographic near-misses of the same kind.
#
# An earlier version also had play->pray, music->musik and explain->explaine,
# which were invented rather than observed. They are removed because they were
# actively harmful: "pray taylor swift" is not a music request in any language,
# so training and grading on it teaches nothing and costs real accuracy. A
# mutation has to be a mistake the recogniser actually makes; otherwise it is
# just noise wearing a costume.
ASR_SLIPS = [
    ("meeting", "meaning"),          # observed, repeatedly
    ("meeting", "meting"),
    ("calendar", "calender"),
    ("microphone", "micro phone"),
    ("camera", "camara"),
    ("standup", "stand up"),
    ("schedule", "shedule"),
]


def mutate(text: str, rng: random.Random, variant: int = 0) -> str:
    """Apply a *specific* kind of damage per variant rather than rolling dice.

    Randomising independently produced mostly-identical copies which then
    deduplicated away, leaving 243 rows instead of 600 and, worse, leaving the
    messy cases underrepresented -- exactly the ones that matter.
    """
    variant %= 5
    if variant == 1:
        text = f"{rng.choice(FILLERS)} {text}"
    elif variant == 2:
        for a, b in ASR_SLIPS:
            if a in text:
                text = text.replace(a, b, 1)
                break
        else:
            text = text.replace("'", "")
    elif variant == 3:
        text = f"{text} {rng.choice(['uh', 'um', 'please', 'thanks', 'yeah'])}"
    elif variant == 4:
        text = f"{rng.choice(FILLERS)} {text}".replace("'", "")
    return " ".join(text.split())


def row(text, intent, style="", query="", when=""):
    return {"text": text, "intent": intent, "style": style,
            "query": query, "when": when}


def build() -> list[dict]:
    rng = random.Random(SEED)
    out: list[dict] = []

    for template in ATTEND:
        for v in range(5):
            when = ""
            text = template
            if "{time}" in template:
                when = rng.choice(TIMES)
                text = template.replace("{time}", when)
            if "{name}" in template:
                when = rng.choice(NAMES)
                text = template.replace("{name}", when)
            out.append(row(mutate(text, rng, v), "attend_meeting", when=when))

    for t in NEXT:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "next_meeting"))
    for t in HARDWARE:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "hardware"))
    for t in HIDE:
        for v in range(4):
            out.append(row(mutate(t, rng, v), "hide"))
    for t, q in MUSIC:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "play_music", query=q))
    for t, q in OPEN:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "open_site", query=q))
    for t, q in NOTE:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "note", query=q))
    for t, q in MODE:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "mode", query=q))
    for t, q in MODEL:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "model", query=q))
    for t, s in STYLE:
        for v in range(5):
            out.append(row(mutate(t, rng, v), "style", style=s))
    for t in ASK:
        for v in range(4):
            out.append(row(mutate(t, rng, v), "ask"))
    for t in UNKNOWN:
        for _ in range(2):
            out.append(row(t, "unknown"))

    # Deduplicate: mutation collides, and a duplicate in eval quietly weights
    # one phrasing more heavily than the rest.
    seen, unique = set(), []
    for r in out:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        unique.append(r)

    rng.shuffle(unique)
    return unique


def main() -> None:
    data = build()
    # Stratified split, so every intent appears in eval in proportion. A random
    # 80/20 on 600 rows can leave an intent with two eval examples, and then
    # its accuracy is measured in 50-point jumps.
    by_intent: dict[str, list[dict]] = {}
    for r in data:
        by_intent.setdefault(r["intent"], []).append(r)

    train, evalset = [], []
    for rows in by_intent.values():
        cut = max(1, int(len(rows) * 0.25))
        evalset += rows[:cut]
        train += rows[cut:]

    for name, rows in (("commands.train.jsonl", train),
                       ("commands.eval.jsonl", evalset)):
        path = OUT / name
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{path.name:24} {len(rows):4d} rows")

    counts: dict[str, int] = {}
    for r in data:
        counts[r["intent"]] = counts.get(r["intent"], 0) + 1
    print("\nby intent:")
    for k in sorted(counts):
        print(f"  {k:16} {counts[k]:4d}")
    print(f"\ntotal {len(data)}")


if __name__ == "__main__":
    main()
