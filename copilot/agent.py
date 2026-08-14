"""The part that decides what a spoken instruction meant, and does it.

Intent classification goes through Gemini rather than keyword matching, for the
same reason the summariser does: "attend my meeting", "join my next call",
"get me into the standup" and "hop into my 10:30" are the same request, and a
keyword table would need every phrasing written down in advance.

The reply Nod speaks is generated in the same call as the intent. Two round
trips -- one to classify, one to phrase -- would double the wait before it
answers, and the wait is the whole feel of the thing.

What it will not do: act on anything from the speaker path. Instructions only
ever arrive on bus.heard, which only the microphone feeds. A meeting playing
through the speakers cannot reach this class, by construction rather than by
filtering. See listen.py.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time

import requests

from . import failure, perf
from .bus import Bus, Suggestion
from . import persona
from .hardware import detect
from .llm import RateGate
from .llm_util import QuotaExhausted, first_candidate_text, post_gemini


# Shares the assistant's model rather than the summariser's, so classification
# and answering draw on the same per-model daily allowance and a busy meeting
# cannot exhaust the assistant. See brain.MODEL.
MODEL = os.environ.get("GEMINI_ASK_MODEL", "gemini-3.5-flash-lite")
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

SYSTEM_TEMPLATE = """You are the intent parser for Nod, a desktop assistant the user \
talks to out loud. You receive one spoken instruction, already transcribed, and \
you decide what was being asked for.

The transcript comes from speech recognition and will contain errors. Infer \
through them: "attend my meaning" is "attend my meeting", "join my ten thirty" \
is a meeting at 10:30.

Intents:
  attend_meeting  join a call, now or the next one on the calendar
  next_meeting    asking what is coming up, without wanting to join it
  hardware        which microphone, camera or speakers will be used
  hide            hide, dismiss, or shut up
  ask             any actual question — facts, prices, news, definitions,
                  calculations, how something works, general conversation
  play_music      play music, put a song on, play a named track or artist.
                  Still play_music when a site is named as the means of doing
                  it: "go to music.youtube.com and play me a song" is a
                  request for music, not a request to open a website.
  open_site       open a website or app with no other action attached:
                  "open youtube", "go to github.com", "pull up gmail"
  note            remember something about the user's own work, so it can be
                  used later when suggesting answers in a meeting:
                  "note that I'm working on the batch layer", "remember the
                  SOC2 audit slipped"
  mode            turn your own listening on or off: "start listening to my
                  meeting", "stop summarising", "watch my screen", "what mode
                  are you in"
  model           which brain to use, or which is in use: "what model are you
                  using", "use the local model", "switch to gemini"

Three intents use overlapping words and mean entirely different things. Read
the distinction carefully, because getting it wrong is disruptive:

  attend_meeting  GET INTO a call. join, attend, sit in on, hop into, take.
                  You open a browser and join a Google Meet. Something happens
                  in the outside world and other people see it.

  mode            START OR STOP PAYING ATTENTION to audio that is already
                  playing. listen, summarise, transcribe, watch my screen,
                  take notes. Nothing is joined and nobody else notices.
                  "start listening to my meeting" is this, not attend_meeting
                  — they are asking you to listen, not to join.

  hide            STOP TALKING OR SHOWING YOURSELF. hide, shut up, be quiet,
                  go away, dismiss, stop talking. This is about your voice and
                  your overlay, never about listening. A bare "stop" or "quiet"
                  is hide.
  style           asking you to change *how* you explain, not what about:
                  "explain like I'm five", "talk to me like a kid", "simpler",
                  "more technical", "in plain English"
  unknown         only when the transcript is too garbled to be a question

`ask` is the common case and the default. If it is a question of any kind, or \
even just chat, it is `ask`. Do not answer it yourself and do not put an answer \
in `reply` — something else handles that, and a guess from you would be spoken \
before the real answer arrives. For `ask`, leave `reply` empty.

Reserve `unknown` for genuine nonsense. "What is the airspeed velocity of an \
unladen swallow" is a question, so it is `ask`, not unknown.

{reply_instructions}

If the intent is unknown, say so plainly in one short sentence and do not guess.

`when` is any time or meeting name mentioned, verbatim and lowercase, or empty.

`style` is set only for the style intent: one of kid, plain, technical. A \
request to "explain like I'm five", "like a kid", "simply" is kid. "Plain \
English", "normally", "less simple" is plain. "More technical", "properly", \
"in detail" is technical.

`query` is set for play_music, open_site, note, mode and model.
  mode        "on" to start listening to the meeting, "off" to stop, or empty
              when they are only asking what mode you are in.
  model       "local", "gemini", or empty when only asking which is in use.
  play_music  the song, artist or genre, or empty for no preference. "play me
              some music" leaves it empty; "play Bohemian Rhapsody by Queen"
              gives "Bohemian Rhapsody Queen". Never put the website in here --
              "go to music.youtube.com and play some jazz" gives "jazz".
              Never put a browser in here either. Brave, Chrome, Edge and
              Firefox are how the request gets carried out, never the thing
              being asked for: "play a music in Brave" gives "", not "brave",
              and "go to Facebook using Brave browser" is open_site with
              "facebook".
  open_site   the site named: "open youtube" gives "youtube", "go to
              github.com" gives "github.com".
  note        what to remember, with the instruction stripped: "note that I'm
              working on the batch layer" gives "working on the batch layer"."""

def system_prompt(name: str | None = None) -> str:
    """The classifier prompt, with the persona's reply rules spliced in."""
    return SYSTEM_TEMPLATE.format(
        reply_instructions=persona.reply_instructions(name))


# The default, kept as a module attribute because datasets/benchmark.py and
# copilot/local_intent.py both import SYSTEM directly.
SYSTEM = system_prompt()

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING",
                   "enum": ["attend_meeting", "next_meeting", "hardware",
                            "hide", "ask", "play_music", "open_site", "note",
                            "mode", "model", "style", "unknown"]},
        "reply": {"type": "STRING"},
        "when": {"type": "STRING"},
        # Not an enum. An enum containing "" is rejected outright --
        # `response_schema.properties[style].enum[3]: cannot be empty` -- and
        # since `style` is only meaningful for one of eight intents, every
        # other intent needs to leave it blank. A plain string plus the check
        # in _do_style is the way to have both.
        "style": {"type": "STRING"},
        "query": {"type": "STRING"},
    },
    "required": ["intent", "reply", "when", "style", "query"],
    "propertyOrdering": ["intent", "reply", "when", "style", "query"],
}

# Six turns is about two minutes of talking, which covers "explain that
# differently" and the follow-up after it without carrying an hour-old topic
# into an unrelated question -- and without quietly growing the prompt, and the
# bill, on a free key.
HISTORY_TURNS = 6

# The browser is *how* Nod does things, never the thing being asked for. Naming
# it is natural -- "play a music in Brave", "go to Facebook using Brave
# browser" -- and the classifier duly handed back query="brave", so Nod
# announced "Playing brave." and searched YouTube Music for the word. Stripped
# here as well as discouraged in the prompt, because a model that gets this
# right nine times out of ten still gets it wrong in front of someone.
BROWSER_NAMES = {"brave", "chrome", "chromium", "edge", "msedge", "firefox",
                 "browser"}
# The words that attach a browser to a request, dropped with it so "in brave"
# does not leave a stray "in" behind.
BROWSER_WORDS = BROWSER_NAMES | {"the", "my", "in", "on", "using", "with", "via"}


def strip_browser(query: str) -> str:
    """Drop browser names and the words that attach them to a request."""
    kept = [w for w in query.split() if w.strip(".,").lower() not in BROWSER_WORDS]
    return " ".join(kept).strip()


def names_browser(query: str) -> bool:
    return any(w.strip(".,").lower() in BROWSER_NAMES for w in query.split())


class Agent(threading.Thread):
    """Consumes bus.heard, decides, speaks, and puts a card on the HUD."""

    daemon = True

    # Under the free tier's ~15/min for flash-lite, and deliberately well
    # under: a single question is a classify call plus up to MAX_ROUNDS tool
    # rounds, so the worst case for one spoken sentence is five requests.
    RPM_LIMIT = 10

    def __init__(self, bus: Bus, speaker, api_key: str | None = None,
                 camera_hint: str | None = None, local_intent: bool = False,
                 allow_unconfirmed_camera: bool = False,
                 persona_name: str | None = None) -> None:
        super().__init__(name="agent")
        self.bus = bus
        self.speaker = speaker
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.camera_hint = camera_hint
        self.local_intent = local_intent
        # Off by default: Nod refuses to join rather than join with a camera it
        # could not verify. Missing a meeting is recoverable; broadcasting a
        # room to your colleagues is not.
        self.allow_unconfirmed_camera = allow_unconfirmed_camera
        # DevTools target id of the tab currently playing music, so the next
        # song can replace it instead of playing alongside it.
        self._music_target = ""
        self.session = requests.Session()
        # Gemini "contents" turns, so they can be replayed straight back.
        self.history: list[dict] = []
        self.style = "plain"
        self._browser = None      # one Chrome, reused for meetings and music
        # "local" pins answering to Ollama; None means Gemini with fallback.
        self.force_model: str | None = None
        # When Gemini is being rested, as a timestamp rather than a boolean.
        #
        # This was `fallback_local = True`, set on the first 429 and never
        # cleared, so a single per-minute rate limit -- which clears in under a
        # minute -- moved answering to the local model for the rest of the
        # session and announced it as being out of quota for the day. Five
        # questions in quick succession was enough to trigger it, because one
        # question can spend a classify call plus four tool rounds on the same
        # model's per-minute allowance.
        self._gemini_rested_until = 0.0
        self._announced_fallback = False
        # Client-side pacing, so the limit is usually not reached at all. A 429
        # costs a full round trip and returns nothing; declining to send costs
        # nothing. Same reasoning as llm.RateGate, which the summariser has had
        # all along -- the assistant simply never got one.
        self.gate = RateGate(self.RPM_LIMIT)
        # The transcript of the instruction currently being acted on.
        self._heard = ""
        # Personality, resolved once: the prompt is rebuilt per persona
        # rather than per request.
        self.persona = persona_name
        self.system = system_prompt(persona_name)

    # -- intent ----------------------------------------------------------
    def _classify(self, text: str) -> dict | None:
        if self.local_intent:
            from . import local_intent
            try:
                return local_intent.classify(text, self.system)
            except Exception as exc:
                # Falling through to the API rather than failing: Ollama not
                # running is a normal state, and the assistant going deaf
                # because a background service stopped is not acceptable.
                self.bus.say(f"local intent unavailable ({type(exc).__name__})")

        body = {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 200,
                "responseMimeType": "application/json",
                "responseSchema": SCHEMA,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        try:
            payload = post_gemini(self.session, ENDPOINT, self.api_key, body,
                                  timeout=12)
        except QuotaExhausted as exc:
            self._rest_gemini(exc)
            # Retry the classification on the local model rather than giving
            # up on the sentence. Being rate limited for a minute should not
            # cost the user the thing they just said.
            try:
                from . import local_intent

                return local_intent.classify(text, self.system)
            except Exception:
                if not exc.per_day:
                    self.bus.speech.put("Give me a moment, I'm going too fast.")
                return None
        except Exception as exc:
            self.bus.say(f"agent: {exc}")
            return None

        raw = first_candidate_text(payload)
        return json.loads(raw) if raw.strip() else None

    # -- actions ---------------------------------------------------------
    def _card(self, lead: str, points: list[str], kind: str = "agent") -> None:
        # Six rather than the summariser's three: a recipe cut off after three
        # steps is not a summary of the recipe, it is the first half of one.
        #
        # Every agent card carries the transcript that caused it. Read from
        # self._heard rather than passed in, so the background threads that
        # raise cards long after dispatch -- music, open-site, ask -- get it
        # without every call site having to thread it through.
        self.bus.put_drop_oldest(
            self.bus.suggestions,
            Suggestion(lead=lead, points=points[:6], kind=kind,
                       heard=self._heard))

    def _remember(self, question: str, answer: str) -> None:
        self.history.append({"role": "user", "parts": [{"text": question}]})
        self.history.append({"role": "model", "parts": [{"text": answer}]})
        del self.history[:-2 * HISTORY_TURNS]

    def _do_hardware(self, reply: str) -> str:
        hw = detect(prefer_camera=self.camera_hint)
        self._card("Devices Nod will use", hw.card_points())
        return hw.spoken()

    def _do_attend(self, when: str, reply: str) -> str:
        from . import agenda

        hw = detect(prefer_camera=self.camera_hint)
        try:
            meeting = agenda.pick(when)
        except agenda.CalendarNotConfigured as exc:
            self._card("Calendar not connected", [str(exc)], kind="reply")
            return "Your calendar isn't connected yet."
        except Exception as exc:
            self._card("Calendar lookup failed", [type(exc).__name__], kind="reply")
            return "I couldn't reach your calendar."

        if not meeting:
            self._card("Nothing to join", ["No meeting with a Meet link is "
                                           "running or starting soon"], kind="reply")
            return "I don't see a meeting to join right now."

        # Card first, join second: it takes a few seconds, and the whole point
        # of the card is to show which devices are about to be used *before*
        # anything is live.
        self._card(f"Attending — {meeting.title}", hw.card_points())

        threading.Thread(target=self._join, args=(meeting,),
                         name="meet-join", daemon=True).start()

        return (f"Joining {meeting.title} now. "
                f"{hw.spoken()}")

    def _join(self, meeting) -> None:
        """Runs off the agent thread so 'hey Nod' still answers while joining."""
        from . import meet

        try:
            result = meet.join(
                meeting.link,
                require_camera_off=not self.allow_unconfirmed_camera)
        except Exception as exc:
            result = meet.JoinResult(False, f"{type(exc).__name__}: {exc}")

        if result.ok and result.state == "waiting":
            # Asked to join, held in the waiting room. This used to be reported
            # as "You're in", which is a confident false statement about whether
            # other people can see and hear you.
            self._card(f"Waiting to be let in — {meeting.title}",
                       ["Microphone off",
                        "Camera off" if result.camera_off else "CAMERA UNCONFIRMED",
                        "Waiting for the host"])
            self.bus.speech.put("I asked to join. You're waiting to be let in.")
        elif result.ok:
            cam_line = "Camera off" if result.camera_off else "CAMERA UNCONFIRMED"
            self._card(f"In — {meeting.title}",
                       ["Microphone off", cam_line, "Joined by Nod"])
            self.bus.speech.put(
                "You're in, muted and camera off." if result.camera_off
                else "You're in and muted, but check your camera."
            )
        else:
            self._card(f"Could not join — {meeting.title}",
                       [failure.card_detail(result.detail)], kind="reply")
            self.bus.speech.put(failure.spoken(result.detail,
                                               "I couldn't join that meeting."))

    def _do_ask(self, question: str) -> None:
        """Answered off-thread: a lookup can take a few seconds, and up to
        twenty when a search provider is throttling us. Blocking the agent
        thread for that would mean 'hey Nod' goes unanswered meanwhile."""
        from . import brain

        # No point line: the echo row already shows the question verbatim, and
        # printing the same sentence twice on a card read in one second is
        # worse than printing it once.
        self._card("Looking that up", [])

        def work():
            local_only = self.force_model == "local" or self._resting()
            answer = sources = steps = None

            if not local_only:
                try:
                    if not self.gate.allow():
                        # Our own limiter said no. Better to pause a moment
                        # than to spend a round trip earning a 429 that would
                        # then rest Gemini for a minute.
                        time.sleep(2.0)
                    answer, sources, steps = brain.ask(
                        question, self.api_key, self.style, self.history,
                        persona_name=self.persona)
                except QuotaExhausted as exc:
                    local_only = True
                    self._rest_gemini(exc)
                except Exception as exc:
                    self.bus.say(f"ask: {type(exc).__name__}")
                    self._card("Lookup failed", [failure.card_detail(exc)],
                               kind="reply")
                    self.bus.speech.put(failure.spoken(exc,
                                                       "I couldn't look that up."))
                    return

            if local_only:
                from . import local_intent
                try:
                    answer = local_intent.answer(question)
                except Exception as exc:
                    self._card("No model available",
                               [failure.card_detail(exc)], kind="reply")
                    self.bus.speech.put(failure.spoken(exc))
                    return
                sources, steps = ["answered locally, not looked up"], []

            self._remember(question, answer)
            # Steps beat sources on the card when there are any: after a
            # spoken how-to, what you want on screen is the thing you are
            # about to follow, not where it came from.
            self._card(question[:74] if steps else (answer[:74] or "No answer"),
                       steps or sources[:2] or ["answered without a lookup"])
            self.bus.speech.put(answer)

        threading.Thread(target=work, name="ask", daemon=True).start()

    # Spoken site names that are not domains. Anything containing a dot is
    # treated as a domain directly, so this only has to cover the shorthands
    # people actually say out loud.
    SITES = {
        "youtube": "https://www.youtube.com",
        "youtube music": "https://music.youtube.com",
        "music": "https://music.youtube.com",
        "gmail": "https://mail.google.com",
        "email": "https://mail.google.com",
        "google": "https://www.google.com",
        "calendar": "https://calendar.google.com",
        "google calendar": "https://calendar.google.com",
        "drive": "https://drive.google.com",
        "github": "https://github.com",
        "meet": "https://meet.google.com",
        "maps": "https://maps.google.com",
        "facebook": "https://www.facebook.com",
        "messenger": "https://www.messenger.com",
        "twitter": "https://x.com",
        "x": "https://x.com",
        "reddit": "https://www.reddit.com",
        "linkedin": "https://www.linkedin.com",
        "shopee": "https://shopee.ph",
        "lazada": "https://www.lazada.com.ph",
        "netflix": "https://www.netflix.com",
        "spotify": "https://open.spotify.com",
        "chatgpt": "https://chatgpt.com",
        "claude": "https://claude.ai",
        "ai studio": "https://aistudio.google.com",
        "stack overflow": "https://stackoverflow.com",
        "whatsapp": "https://web.whatsapp.com",
        "teams": "https://teams.microsoft.com",
        "outlook": "https://outlook.office.com",
        "notion": "https://www.notion.so",
        "jira": "https://www.atlassian.com/software/jira",
    }

    def _get_browser(self):
        """The shared Chrome, relaunched if the last one has gone away.

        The old check was `if self._browser is None`, which never noticed the
        user closing the window -- every later music or open command then hit a
        dead DevTools port. Chrome closing should cost one relaunch, not the
        rest of the session.
        """
        from . import meet

        if self._browser is not None and not self._browser.is_alive():
            self.bus.say("browser: gone, relaunching")
            self._browser = None
        if self._browser is None:
            self._browser = meet.Browser()
        return self._browser

    def _resolve_site(self, query: str) -> str | None:
        q = query.lower().strip().strip(".")
        if not q:
            return None
        if q in self.SITES:
            return self.SITES[q]
        # A bare domain: "github.com", "news.ycombinator.com".
        token = q.split()[-1]
        if "." in token and " " not in token:
            return token if token.startswith("http") else f"https://{token}"
        # Last chance: "open the youtube page" -> match a known name inside it.
        for name, url in self.SITES.items():
            if name in q:
                return url
        return None

    def _do_open(self, query: str) -> str:
        from . import meet

        # "open my brave browser" names the browser and no site at all, which
        # used to resolve to nothing and get answered with "I don't know that
        # site" -- about the browser Nod was already going to use.
        site = strip_browser(query)
        url = self._resolve_site(site)
        if not url and names_browser(query) and not site:
            url = "about:blank"
        if not url:
            # Guessing a domain from an unrecognised word lands on parked
            # pages and typo-squats; saying so is better than opening one.
            return "I don't know that site. Try naming the domain."

        def work():
            try:
                tab = self._get_browser().open(url)
                # Drops the websocket only. The page stays open in Chrome --
                # closing the debugger connection is not closing the tab.
                tab.close()
                self._card("Opened", [url.replace("https://", ""), f"in Nod's {meet.browser_name()}"])
            except Exception as exc:
                self._card("Couldn't open it", [failure.card_detail(exc)],
                           kind="reply")
                self.bus.speech.put(failure.spoken(exc, "I couldn't open that."))

        self._card("Opening", [url.replace("https://", "")])
        threading.Thread(target=work, name="open-site", daemon=True).start()
        if url == "about:blank":
            return f"Opening {meet.browser_name()}."
        return f"Opening {site}." if site else "Opening it."

    def _do_mode(self, query: str) -> str:
        want = query.lower().strip()
        meeting = self.bus.meeting_on.is_set()

        if want in ("on", "start", "yes"):
            if not meeting:
                self.bus.meeting_on.set()
            self._card("Listening to your meeting",
                       ["Speaker audio, screen text, summaries",
                        "First start takes ~10s to load Whisper"])
            return "Listening to your meeting now."

        if want in ("off", "stop", "no"):
            if meeting:
                self.bus.meeting_on.clear()
            self._card("Meeting listening off",
                       ["Still listening for 'hey Nod'"])
            return "Stopped listening to your meeting."

        # No preference given: report.
        state = "listening to your meeting" if meeting else "not listening to your meeting"
        self._card("Mode",
                   [f"Meeting — {'on' if meeting else 'off'}",
                    f"Agent — {'on' if self.bus.agent_on.is_set() else 'off'}"])
        return f"I'm {state}."

    def _resting(self) -> bool:
        """Is Gemini being rested after a rate limit?"""
        return time.time() < self._gemini_rested_until

    def _rest_gemini(self, exc: QuotaExhausted) -> None:
        """Step aside for exactly as long as Google asked, then try again.

        The old behaviour was permanent for the session and announced as being
        out of quota for the day. Both were usually wrong: a per-minute limit
        is the common case by far, it clears in under a minute, and saying
        otherwise sent people looking at their billing.
        """
        self._gemini_rested_until = time.time() + exc.retry_after
        perf.mark("gemini.rested",
                  f"per_day={exc.per_day} for={exc.retry_after:.0f}s")
        self.bus.say(f"gemini: resting {exc.retry_after:.0f}s ({exc})")

        if exc.per_day:
            # Worth saying out loud once: this one really does last.
            if not self._announced_fallback:
                self._announced_fallback = True
                self.bus.speech.put(
                    "I'm out of Gemini quota for today, so I'll answer locally. "
                    "I can't look things up.")
        else:
            # Deliberately silent. A one-minute pause that fixes itself is not
            # worth interrupting someone to explain, and the answer they asked
            # for is still coming -- just from the local model.
            self.bus.say("gemini: rate limited, using the local model briefly")

    def _brain_name(self) -> str:
        if self.force_model == "local" or self._resting():
            return "the local model"
        return "Gemini"

    def _do_model(self, query: str) -> str:
        want = query.lower().strip()
        if "local" in want or "ollama" in want:
            self.force_model = "local"
            self._card("Answering locally", ["qwen2.5:3b on your GPU",
                                             "No API quota used"])
            return "Using the local model."
        if "gemini" in want or "cloud" in want or "flash" in want:
            self.force_model = None
            # Asking for Gemini explicitly also ends any rate-limit rest early.
            self._gemini_rested_until = 0.0
            self._card("Answering with Gemini",
                       ["Falls back to local if quota runs out"])
            return "Using Gemini."

        self._card("Model",
                   [f"Answers — {self._brain_name()}",
                    f"Commands — {'local' if self.local_intent else 'Gemini'}"])
        return f"I'm answering with {self._brain_name()}."

    def _do_note(self, query: str, spoken: str) -> str:
        from . import workctx

        note = query or spoken
        if workctx.append(note):
            self._card("Noted", [note[:68], "added to your work context"])
            return "Noted."
        self._card("Couldn't save that note", ["check ~/.nod is writable"],
                   kind="reply")
        return "I couldn't save that."

    def _do_style(self, style: str) -> str:
        self.style = style if style in ("kid", "plain", "technical") else "plain"
        wording = {"kid": "Alright, keeping it simple.",
                   "plain": "Okay, plain English.",
                   "technical": "Okay, I'll go into detail."}[self.style]
        # Re-answer the last question in the new style rather than just
        # acknowledging: "explain it like a kid" is a request for the
        # explanation again, not a settings change.
        last = next((t["parts"][0]["text"] for t in reversed(self.history)
                     if t["role"] == "user"), "")
        if last:
            self.bus.speech.put(wording)
            self._do_ask(last)
            return ""
        return wording

    def _do_music(self, query: str) -> str:
        from . import meet

        # "play a music in brave" must not become a search for "brave".
        query = strip_browser(query)

        def work():
            try:
                browser = self._get_browser()
                # Stop what is already playing before starting anything else.
                # Each request opens its own tab and nothing ever closed the
                # last one, so a second song played on top of the first --
                # through the speakers, which the meeting capture is recording
                # and Whisper is transcribing.
                if self._music_target:
                    browser.close_target(self._music_target)
                    self._music_target = ""
                result = meet.play_music(query, browser)
            except Exception as exc:
                result = meet.JoinResult(False, f"{type(exc).__name__}: {exc}")

            if result.ok:
                self._music_target = result.target_id
                self._card("Playing", [result.detail.replace("playing ", ""),
                                       "YouTube Music"])
            else:
                # Detail on the card, one sentence in the air. Reading a
                # ConnectionError aloud takes fifteen seconds and helps nobody.
                self._card("Couldn't play that",
                           [failure.card_detail(result.detail)], kind="reply")
                self.bus.speech.put(failure.spoken(result.detail,
                                                   "I couldn't start that."))

        self._card("Opening YouTube Music", [query or "something at random"])
        threading.Thread(target=work, name="music", daemon=True).start()
        return f"Playing {query}." if query else "Putting something on."

    def _do_next(self, reply: str) -> str:
        from . import agenda

        try:
            meetings = agenda.upcoming()
        except agenda.CalendarNotConfigured:
            return "Your calendar isn't connected yet."
        except Exception:
            return "I couldn't reach your calendar."

        ahead = [m for m in meetings if m.minutes_away > -5]
        if not ahead:
            self._card("Nothing coming up", ["Calendar is clear for now"])
            return "Nothing on your calendar for the next few hours."

        nxt = ahead[0]
        self._card(f"Next — {nxt.title}",
                   [f"At {nxt.start.astimezone().strftime('%I:%M %p').lstrip('0')}",
                    "Has a Meet link" if nxt.link else "No Meet link"])
        if nxt.in_progress:
            return f"{nxt.title} is running now."
        mins = int(nxt.minutes_away)
        when_txt = f"in {mins} minutes" if mins < 60 else f"at {nxt.spoken_time()}"
        return f"{nxt.title}, {when_txt}."

    # -- loop ------------------------------------------------------------
    def run(self) -> None:
        if not self.api_key:
            # Without a key the agent used to return here, which killed the
            # only consumer of bus.heard. Everything upstream kept working --
            # the wake word answered, "Yes?" was spoken -- and then commands
            # went into a queue nobody was reading. That is indistinguishable
            # from a hang, and the status line still showed the ears' startup
            # message because nothing after it ever ran.
            #
            # --local-intent classifies on Ollama and needs no key at all, so
            # it now carries on and answers locally.
            if not self.local_intent:
                self.bus.say("agent: GEMINI_API_KEY not set — commands ignored")
                return
            self.bus.say("agent: no API key, answering locally")
            # No key is not a rate limit -- it is permanent until one is set,
            # so pin the local model rather than resting Gemini on a timer.
            self.force_model = "local"

        while not self.bus.stop.is_set():
            try:
                text = self.bus.heard.get(timeout=0.2)
            except queue.Empty:
                continue
            if not text.strip():
                continue

            self._heard = text.strip()
            self.bus.say(f"heard: {text}")
            perf.mark("classify.start", text[:40])
            try:
                data = self._classify(text)
            except Exception as exc:
                self.bus.say(f"agent: {type(exc).__name__}")
                self._card("Couldn't work out what that meant",
                           [failure.card_detail(exc)], kind="reply")
                self.bus.speech.put("Sorry, I didn't catch that.")
                continue

            if not data:
                self._card("Couldn't work out what that meant", [], kind="reply")
                self.bus.speech.put("Sorry, I didn't catch that.")
                continue

            intent = data.get("intent", "unknown")
            reply = (data.get("reply") or "").strip()
            when = (data.get("when") or "").strip()
            perf.mark("classify.done", intent)

            if intent == "style":
                reply = self._do_style((data.get("style") or "").strip())
                if reply:
                    self.bus.speech.put(reply)
                continue
            if intent == "play_music":
                reply = self._do_music((data.get("query") or "").strip())
                if reply:
                    self.bus.speech.put(reply)
                continue
            if intent == "open_site":
                reply = self._do_open((data.get("query") or "").strip())
                if reply:
                    self.bus.speech.put(reply)
                continue
            if intent == "note":
                reply = self._do_note((data.get("query") or "").strip(), text)
                if reply:
                    self.bus.speech.put(reply)
                continue
            if intent == "mode":
                reply = self._do_mode((data.get("query") or "").strip())
                if reply:
                    self.bus.speech.put(reply)
                continue
            if intent == "model":
                reply = self._do_model((data.get("query") or "").strip())
                if reply:
                    self.bus.speech.put(reply)
                continue
            if intent == "ask":
                # Answers arrive from brain.py, so nothing is spoken here --
                # the classifier's own reply would land first and pre-empt it.
                self._do_ask(text)
                continue
            if intent == "hardware":
                reply = self._do_hardware(reply)
            elif intent == "attend_meeting":
                reply = self._do_attend(when, reply)
            elif intent == "next_meeting":
                reply = self._do_next(reply)
            elif intent == "unknown":
                # The card that most needs the echo row. Nod is about to say it
                # did not understand, and nine times out of ten the reason is
                # sitting in what it heard -- "Koto Facebook" explains itself.
                # Without a card there was nowhere for that to be shown.
                self._card("Didn't catch that", ["Say it again, or rephrase"],
                           kind="reply")
            elif intent == "hide":
                # "hide", "shut up", "be quiet". Silence means silence: drop
                # whatever is queued and stop the sentence in progress, and do
                # not say anything back. Acknowledging a request to stop
                # talking by talking is how this used to read as ignoring you.
                self.bus.hud_hide.set()
                self.speaker.interrupt()
                continue

            if reply:
                self.bus.speech.put(reply)
