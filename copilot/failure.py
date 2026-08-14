"""Turning things that went wrong into something worth hearing.

This exists because of one specific moment: Nod failed to start a song and read
this out loud, in full, at normal speaking pace:

    ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=60348):
    Max retries exceeded with url: /json/new?https://music.youtube.com/...

Every word of that is true and none of it is useful to someone waiting for
music. Worse, it takes about fifteen seconds to say, during which the assistant
is unusable.

So failures now split in two. The *spoken* half is one short sentence about
what did not happen, in the terms the user was thinking in -- they asked for
music, so the answer is about music, not about an HTTP connection pool. The
*written* half keeps the diagnostic detail and goes on the overlay card, where
it can be read in a glance, ignored, or quoted into a bug report.

Classification is by cause rather than by exception class, because the classes
lie: a dead DevTools port surfaces as requests.ConnectionError, which is
indistinguishable at the type level from a failed search. What the user needs
to know is "the browser went away", and that is inferable from the message.
"""

from __future__ import annotations

import re

# Kept short deliberately -- these are spoken, and a sentence the user has
# already heard twice should be over quickly.
GENERIC = "I couldn't do that."

_RULES: list[tuple[re.Pattern, str]] = [
    # Order matters: first match wins, so the specific ones come first.
    (re.compile(r"quota|resource_exhausted|429", re.I),
     "I'm out of Gemini quota."),
    # Before the general connection rule: a refused connection to 11434 is
    # Ollama being down, and saying "I couldn't reach the browser" for it sends
    # you looking in entirely the wrong place.
    (re.compile(r"ollama|11434", re.I),
     "The local model isn't running."),
    (re.compile(r"connection|max retries|refused|10061|urlopen|"
                r"failed to establish", re.I),
     "I couldn't reach the browser."),
    (re.compile(r"chrome (not found|did not|exited)|debugging port", re.I),
     "I couldn't start the browser."),
    (re.compile(r"timeout|timed out", re.I),
     "That took too long."),
    (re.compile(r"pre-join|join button|not signed in", re.I),
     "I couldn't get into that meeting."),
    # Before the microphone rule, not after it. The camera detail string also
    # contains "could not confirm", so with the old ordering a camera refusal
    # was spoken as a sentence about the microphone -- sending the user to check
    # entirely the wrong thing about a call they are not in.
    (re.compile(r"camera", re.I),
     "I couldn't confirm your camera was off, so I didn't join."),
    (re.compile(r"microphone|could not confirm", re.I),
     "I couldn't confirm the microphone was muted, so I didn't join."),
    (re.compile(r"no playable result|no results", re.I),
     "I couldn't find that."),
    (re.compile(r"calendar|oauth|credentials|ics", re.I),
     "I couldn't reach your calendar."),
]


def spoken(detail: object, fallback: str = GENERIC) -> str:
    """One short sentence to say out loud about `detail`.

    `detail` may be an exception, a JoinResult detail string, or anything else
    with a useful str(). Never returns the input verbatim -- that is the entire
    point -- so a new failure mode produces a bland sentence rather than a
    paragraph of machinery.
    """
    text = str(detail or "")
    for pattern, phrase in _RULES:
        if pattern.search(text):
            return phrase
    return fallback


def card_detail(detail: object, limit: int = 68) -> str:
    """The same failure, trimmed to fit one line of the overlay.

    Truncated rather than summarised: what makes a diagnostic useful is being
    literal, and the first 68 characters of an exception are almost always the
    part that identifies it.
    """
    text = " ".join(str(detail or "").split())
    if not text:
        return "no detail"
    return text if len(text) <= limit else text[: limit - 1] + "…"
