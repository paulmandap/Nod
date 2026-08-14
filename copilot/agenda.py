"""Reading the Google Calendar, so "attend my meeting" knows which meeting.

Named agenda rather than calendar on purpose: a module called `calendar.py`
shadows the standard library one for anything doing a non-absolute import, and
that is a genuinely nasty afternoon to debug for no benefit.

Credentials live outside the repo, under ~/.nod:

    ~/.nod/credentials.json   the OAuth client you download from Google Cloud
    ~/.nod/token.json         written after you approve in the browser, refreshed
                              automatically thereafter

Neither belongs in version control, which is why neither is written anywhere
near the project directory. The scope is calendar.readonly: Nod needs to know
what is on the schedule, and nothing about joining a call requires the ability
to change it.

The Meet link is read from `hangoutLink` first and `conferenceData` second.
Both are real -- older events carry only the former, and events created through
the newer conferencing flow sometimes carry only the latter -- so checking one
of them misses meetings that look fine in the web UI.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

NOD_HOME = Path(os.environ.get("NOD_HOME", Path.home() / ".nod"))
CREDENTIALS = Path(os.environ.get("NOD_GOOGLE_CREDENTIALS",
                                  NOD_HOME / "credentials.json"))
TOKEN = NOD_HOME / "token.json"

# Matched to the exact shape of a Meet code (xxx-xxxx-xxx) rather than "any run
# of letters and dashes". ICS folds long lines and unfolding rejoins them with
# no separator, so a loose pattern happily swallows whatever word follows the
# link and produces a URL that 404s.
_MEET_RE = re.compile(
    r"https://meet\.google\.com/(?:[a-z]{3}-[a-z]{4}-[a-z]{3}|lookup/[a-z0-9]+)",
    re.I,
)


@dataclass
class Meeting:
    title: str
    start: dt.datetime
    end: dt.datetime
    link: str | None = None

    @property
    def minutes_away(self) -> float:
        now = dt.datetime.now(dt.timezone.utc)
        return (self.start - now).total_seconds() / 60.0

    @property
    def in_progress(self) -> bool:
        now = dt.datetime.now(dt.timezone.utc)
        return self.start <= now <= self.end

    def spoken_time(self) -> str:
        local = self.start.astimezone()
        hour = local.strftime("%I").lstrip("0") or "12"
        minute = local.strftime("%M")
        return f"{hour} {minute}" if minute != "00" else hour


class CalendarNotConfigured(RuntimeError):
    pass


class CalendarNeedsConsent(CalendarNotConfigured):
    """An OAuth client is configured, but nobody has approved it yet.

    A subclass, so every existing `except CalendarNotConfigured` -- notably the
    one in Agent._do_attend that speaks the message out loud -- keeps working
    without modification.
    """


def _service(interactive: bool = False):
    """A Calendar API client.

    `interactive` is the fix for a hard hang. run_local_server() blocks until
    the user finishes approving in a browser, or forever if they close the tab,
    and it was being called on the agent thread -- the only consumer of
    bus.heard. While it blocked, the wake word still answered "Yes, boss?" and
    every command after it queued up and was never read. From the outside Nod
    was simply broken, and the terminal it printed the auth URL to may not even
    have been visible.

    So the default is now non-interactive: raise instead of blocking, and let
    the caller speak a sentence about it. Only the settings dialog, on a Qt
    worker thread with a Cancel button, passes interactive=True.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:                     # pragma: no cover
        raise CalendarNotConfigured(f"google libraries missing ({exc})")

    NOD_HOME.mkdir(parents=True, exist_ok=True)
    creds = None

    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS.exists():
                raise CalendarNotConfigured(
                    "set NOD_ICS_URL, or put an OAuth client at "
                    f"{CREDENTIALS}"
                )
            if not interactive:
                raise CalendarNeedsConsent(
                    "Google Calendar needs your approval once. Open Nod's "
                    "settings and click Connect calendar.")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS), SCOPES)
            # Opens a browser once; the loopback receiver is the flow Google
            # still supports for desktop apps. timeout_seconds so an abandoned
            # consent screen eventually gives up instead of pinning the calling
            # thread for the rest of the session.
            creds = flow.run_local_server(port=0, timeout_seconds=180)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _link_for(event: dict) -> str | None:
    if event.get("hangoutLink"):
        return event["hangoutLink"]

    for point in event.get("conferenceData", {}).get("entryPoints", []):
        if point.get("entryPointType") == "video" and point.get("uri"):
            return point["uri"]

    # Some invitations only ever put the link in the body or the location.
    for field in ("location", "description"):
        found = _MEET_RE.search(event.get(field) or "")
        if found:
            return found.group(0)
    return None


def _parse(stamp: str) -> dt.datetime:
    # All-day events carry a bare date; treat them as starting at midnight.
    if len(stamp) == 10:
        return dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _occurrences(block: str, fields: dict, start: dt.datetime, end: dt.datetime,
                 now: dt.datetime, window: dt.datetime):
    """The next occurrence of a recurring event inside the window.

    Returns None when the event does not recur (the caller keeps its own
    times), an empty tuple-ish falsy value when it recurs but not in the
    window, or (start, end) of the next occurrence.

    dateutil rather than a hand-rolled parser. FREQ and INTERVAL are easy;
    BYDAY with a negative ordinal, COUNT versus UNTIL, EXDATE, and a weekly
    rule crossing a daylight-saving boundary are where hand-rolled expanders
    quietly return the wrong day -- and the failure mode here is joining the
    wrong meeting, in front of people.
    """
    rule_raw = fields.get("RRULE")
    if not rule_raw:
        return None

    try:
        from dateutil.rrule import rrulestr
    except ImportError:
        return None                      # no dateutil: behave as before

    try:
        rule = rrulestr(f"RRULE:{rule_raw}", dtstart=start)
    except Exception:
        return None

    # EXDATE lines carry the cancelled instances; a deleted standup is not a
    # meeting to join.
    excluded = set()
    for raw in re.findall(r"^EXDATE[^:\n]*:(.*)$", block, re.M):
        for stamp in raw.split(","):
            try:
                excluded.add(_ics_time(stamp.strip()))
            except ValueError:
                pass

    duration = end - start
    # Start slightly before now so a meeting already running still counts.
    for occ in rule.between(now - dt.timedelta(minutes=30), window, inc=True):
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=dt.timezone.utc)
        if occ in excluded:
            continue
        return occ, occ + duration
    return ()


def _from_ics(url: str, hours: float) -> list[Meeting]:
    """Read the calendar from its private iCal address.

    This exists because creating a Google Cloud project -- which the OAuth path
    requires -- is blocked by policy on many managed work accounts, and no
    amount of code gets around an org policy. The secret ICS address is a
    per-calendar URL that needs no project, no client ID and no consent screen.

    It is a worse feed than the API and you should know how: Google regenerates
    it on a schedule of its own, so a meeting added in the last hour may not be
    in it yet. For "what's on this afternoon" that is fine. For "join the thing
    that was just moved" it is not.

    Parsed by hand rather than with `icalendar` to avoid another dependency for
    what is four fields.
    """
    raw = requests.get(url, timeout=20)
    raw.raise_for_status()

    # Unfold: ICS wraps long lines with a leading space on the continuation.
    text = raw.text.replace("\r\n ", "").replace("\n ", "").replace("\r\n", "\n")
    now = dt.datetime.now(dt.timezone.utc)
    window = now + dt.timedelta(hours=hours)
    meetings: list[Meeting] = []

    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields = dict(re.findall(r"^([A-Z-]+)[^:\n]*:(.*)$", block, re.M))
        start_raw = next((v for k, v in fields.items() if k == "DTSTART"), None)
        end_raw = next((v for k, v in fields.items() if k == "DTEND"), None)
        if not start_raw:
            continue
        try:
            start = _ics_time(start_raw)
            end = _ics_time(end_raw) if end_raw else start + dt.timedelta(hours=1)
        except ValueError:
            continue

        # Recurring events, which is nearly every meeting anyone says "attend
        # my standup" about. Their DTSTART is the date of the *first* instance,
        # so a daily standup created three months ago looked like a meeting
        # three months in the past and was dropped by the window test below.
        # ICS mode therefore reported "nothing to join" for the exact meetings
        # it was most needed for -- confidently, with no error.
        occurrences = _occurrences(block, fields, start, end, now, window)
        if occurrences is not None:
            if not occurrences:
                continue
            start, end = occurrences

        if end < now - dt.timedelta(minutes=30) or start > window:
            continue

        body = " ".join([fields.get("DESCRIPTION", ""),
                         fields.get("LOCATION", ""),
                         fields.get("X-GOOGLE-CONFERENCE", "")])
        link = _MEET_RE.search(body)
        meetings.append(Meeting(
            title=fields.get("SUMMARY", "(no title)").replace("\\,", ","),
            start=start, end=end,
            link=link.group(0) if link else None,
        ))

    return sorted(meetings, key=lambda m: m.start)


def _ics_time(value: str) -> dt.datetime:
    value = value.strip()
    if value.endswith("Z"):
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc)
    if "T" in value:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%S").astimezone(
            dt.timezone.utc)
    return dt.datetime.strptime(value, "%Y%m%d").replace(tzinfo=dt.timezone.utc)


def upcoming(limit: int = 10, hours: float = 12.0) -> list[Meeting]:
    """Events from a little while ago to `hours` ahead, soonest first.

    The window starts in the past deliberately: "attend my meeting" is usually
    said a few minutes *after* something started, not before it.

    Uses the ICS feed when NOD_ICS_URL is set, otherwise the OAuth API.
    """
    ics = os.environ.get("NOD_ICS_URL", "").strip()
    if ics:
        return _from_ics(ics, hours)[:limit]

    svc = _service()
    now = dt.datetime.now(dt.timezone.utc)
    body = svc.events().list(
        calendarId="primary",
        timeMin=(now - dt.timedelta(minutes=30)).isoformat(),
        timeMax=(now + dt.timedelta(hours=hours)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=limit,
    ).execute()

    meetings = []
    for event in body.get("items", []):
        start = event.get("start", {})
        end = event.get("end", {})
        if not start.get("dateTime") and not start.get("date"):
            continue
        if event.get("status") == "cancelled":
            continue
        meetings.append(Meeting(
            title=event.get("summary", "(no title)"),
            start=_parse(start.get("dateTime") or start["date"]),
            end=_parse(end.get("dateTime") or end["date"]),
            link=_link_for(event),
        ))
    return meetings


def pick(when: str = "", joinable_only: bool = True) -> Meeting | None:
    """The meeting "attend my meeting" most likely meant.

    Anything already running wins, then whatever starts soonest within the next
    half hour. A named hint ("standup", "ten thirty") filters by title first.
    """
    meetings = upcoming()
    if joinable_only:
        meetings = [m for m in meetings if m.link]
    if not meetings:
        return None

    if when:
        needle = when.lower().strip()
        named = [m for m in meetings if needle in m.title.lower()]
        if named:
            return named[0]

    live = [m for m in meetings if m.in_progress]
    if live:
        return live[0]

    soon = [m for m in meetings if 0 <= m.minutes_away <= 30]
    return soon[0] if soon else None
