"""The things Nod can actually go and find out.

Gemini's own `google_search` grounding would be the obvious answer and it is
not available: on a free key the request comes back 429 RESOURCE_EXHAUSTED
immediately, with no usage metric attached, which is an entitlement refusal
rather than a limit you can wait out. So Nod does its own lookups and hands the
results back to the model as facts to reason over.

That distinction is the whole point. Asked for the dollar-peso rate with no
tool, Gemini answered "as of May 22, 2024, 1 USD = 58.35 PHP" -- fluent,
confident, two years stale and about 5% wrong. A wrong number delivered in a
calm voice is worse than "I don't know", because there is nothing in the
delivery to warn you.

Search goes through DuckDuckGo's HTML endpoint, which needs no key and no
package. It is scraping, so it is not stable ground: the parser is written to
degrade to an empty list rather than raise, and every caller has to cope with
finding nothing.

Rates come from a purpose-built free API instead of search, because a currency
figure scraped out of a search snippet may be hours old and attached to no
particular timestamp, while er-api returns the number and when it was set.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass

import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15

# One pool for every lookup. These are five different hosts, but a single
# Session still keeps a live connection per host across calls, which matters
# when a question triggers a search and then fetches one of its results.
_session = requests.Session()
_session.headers.update(UA)

# DuckDuckGo answers a steady trickle happily and blocks a burst: about fifteen
# rapid queries earned a 202 "anomaly" page for every subsequent request, and
# the failure is silent -- a normal-looking page with no results in it. It
# cleared on its own after roughly twenty seconds. So requests are spaced out,
# and a block is retried once rather than reported as "nothing found", because
# telling the model there are no results makes it answer from memory, which is
# the exact behaviour this module exists to prevent.
_MIN_GAP = 1.5
_BACKOFF = 6.0
_last_call = 0.0
_gate = threading.Lock()

BRAVE_KEY = os.environ.get("NOD_BRAVE_KEY", "").strip()


def _throttle() -> None:
    global _last_call
    with _gate:
        wait = _MIN_GAP - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _blocked(response) -> bool:
    return response.status_code == 202 or "anomaly" in response.text[:4000].lower()


@dataclass
class Hit:
    title: str
    snippet: str
    url: str = ""

    def as_line(self) -> str:
        return f"{self.title} — {self.snippet}"


def _strip(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def _parse_html_endpoint(page: str, limit: int) -> list[Hit]:
    hits: list[Hit] = []
    for m in re.finditer(
        r'result__a"[^>]*href="(?P<url>[^"]*)"[^>]*>(?P<title>.*?)</a>'
        r'(?P<mid>.*?)result__snippet"[^>]*>(?P<snip>.*?)</a>',
        page, re.S,
    ):
        title, snip = _strip(m.group("title")), _strip(m.group("snip"))
        if title and snip:
            hits.append(Hit(title, re.sub(r"\s+", " ", snip), m.group("url")))
        if len(hits) >= limit:
            break
    return hits


def _parse_lite_endpoint(page: str, limit: int) -> list[Hit]:
    """The lite layout has no result__snippet class; rows are a plain table."""
    hits: list[Hit] = []
    rows = re.findall(r'<a[^>]*class="result-link"[^>]*>(.*?)</a>.*?'
                      r'class="result-snippet"[^>]*>(.*?)</td>', page, re.S)
    for title, snip in rows[:limit]:
        title, snip = _strip(title), _strip(snip)
        if title and snip:
            hits.append(Hit(title, re.sub(r"\s+", " ", snip)))
    return hits


def _brave(query: str, limit: int) -> list[Hit]:
    """Used only if NOD_BRAVE_KEY is set. A real API, so no scraping and no
    rate-limit roulette. Free tier is generous and signing up needs no Google
    Cloud project, which matters on a locked-down work account."""
    r = _session.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={"X-Subscription-Token": BRAVE_KEY,
                 "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    results = (r.json().get("web") or {}).get("results") or []
    return [Hit(x.get("title", ""), _strip(x.get("description", "")),
                x.get("url", "")) for x in results][:limit]


def _duckduckgo(query: str, limit: int) -> list[Hit]:
    for attempt in range(2):
        for url, parse in (
            ("https://html.duckduckgo.com/html/", _parse_html_endpoint),
            ("https://lite.duckduckgo.com/lite/", _parse_lite_endpoint),
        ):
            try:
                _throttle()
                r = _session.post(url, data={"q": query}, headers=UA,
                                  timeout=TIMEOUT)
                if _blocked(r):
                    continue
                r.raise_for_status()
                hits = parse(r.text, limit)
                if hits:
                    return hits
            except Exception:
                continue
        if attempt == 0:
            time.sleep(_BACKOFF)      # let a burst-block expire, then retry
    return []


def wikipedia(query: str, sentences: int = 3) -> Hit | None:
    """Encyclopedic lookup. Reliable and keyless, but only for things that have
    an article -- it cheerfully answers "Manila weather" with an essay on
    weather gods, so it is a fallback for entities, not a search engine."""
    try:
        found = _session.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "srlimit": 1},
            headers=UA, timeout=TIMEOUT,
        ).json()
        results = found.get("query", {}).get("search") or []
        if not results:
            return None
        title = results[0]["title"]
        summary = _session.get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + requests.utils.quote(title, safe=""),
            headers=UA, timeout=TIMEOUT,
        ).json()
        extract = (summary.get("extract") or "").strip()
        if not extract:
            return None
        trimmed = " ".join(re.split(r"(?<=[.!?]) ", extract)[:sentences])
        return Hit(f"Wikipedia: {title}", trimmed,
                   summary.get("content_urls", {})
                          .get("desktop", {}).get("page", ""))
    except Exception:
        return None


def web_search(query: str, limit: int = 5) -> list[Hit]:
    """Best-effort web search. Returns [] rather than raising."""
    if BRAVE_KEY:
        try:
            hits = _brave(query, limit)
            if hits:
                return hits
        except Exception:
            pass                      # fall through to the keyless path

    hits = _duckduckgo(query, limit)
    if hits:
        return hits

    article = wikipedia(query)
    return [article] if article else []


def exchange_rate(base: str, quote: str) -> dict | None:
    """A real rate with the timestamp it was set, or None."""
    base, quote = base.upper().strip(), quote.upper().strip()
    try:
        d = _session.get(f"https://open.er-api.com/v6/latest/{base}",
                         timeout=TIMEOUT).json()
        rate = (d.get("rates") or {}).get(quote)
        if rate:
            return {"base": base, "quote": quote, "rate": rate,
                    "as_of": d.get("time_last_update_utc", "")}
    except Exception:
        pass
    try:
        d = _session.get(
            f"https://api.frankfurter.app/latest?from={base}&to={quote}",
            timeout=TIMEOUT).json()
        rate = (d.get("rates") or {}).get(quote)
        if rate:
            return {"base": base, "quote": quote, "rate": rate,
                    "as_of": d.get("date", "")}
    except Exception:
        pass
    return None


def fetch_page(url: str, max_chars: int = 3000) -> str:
    """Readable text from one page, for when a snippet is not enough."""
    try:
        r = _session.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        return f"(could not fetch: {type(exc).__name__})"
    body = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", r.text)
    return re.sub(r"\s+", " ", _strip(body))[:max_chars]


# --- what the model is told it can call --------------------------------------

DECLARATIONS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use this for anything that "
            "changes over time or that you are not certain of: news, prices, "
            "schedules, who currently holds a position, product details. Prefer "
            "searching over answering from memory."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "the search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "exchange_rate",
        "description": (
            "Get a live currency exchange rate. Use this instead of web_search "
            "for any currency conversion; it returns an exact rate and the time "
            "it was set."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "base": {"type": "STRING", "description": "ISO code, e.g. USD"},
                "quote": {"type": "STRING", "description": "ISO code, e.g. PHP"},
            },
            "required": ["base", "quote"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Read the text of one web page, when a search snippet did not "
            "contain enough detail. Pass a URL returned by web_search."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"url": {"type": "STRING"}},
            "required": ["url"],
        },
    },
]


def run(name: str, args: dict) -> dict:
    """Dispatch one tool call. Always returns a dict, never raises."""
    try:
        if name == "web_search":
            hits = web_search(args.get("query", ""))
            if hits:
                return {"results": [{"title": h.title, "snippet": h.snippet,
                                     "url": h.url} for h in hits]}
            # Say the lookup *failed* rather than that the answer is "nothing".
            # Given an empty result set the model treats the question as
            # unanswered and falls back on memory, which is how a stale rate
            # gets spoken with total confidence.
            return {"error": "search unavailable — do not answer from memory, "
                             "tell the user you could not look it up"}
        if name == "exchange_rate":
            got = exchange_rate(args.get("base", ""), args.get("quote", ""))
            return got or {"error": "rate unavailable"}
        if name == "fetch_page":
            return {"text": fetch_page(args.get("url", ""))}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown tool {name}"}
