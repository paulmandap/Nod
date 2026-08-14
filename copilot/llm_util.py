"""One place that knows how to talk to Gemini.

Three modules call the API for different jobs -- the summariser, the intent
classifier and the question answerer -- and each had grown its own copy of the
same request handling. Two of them had the thinkingConfig retry; the third did
not, which meant pointing GEMINI_MODEL at a 3.5-generation model left the
summariser returning 400 forever with nothing in the logs to explain it.

Two things this centralises:

  thinkingConfig. Whether a model accepts `thinkingBudget: 0` splits by
  generation and cannot be guessed from the name -- 3.1-flash-lite needs it to
  stay fast, 3.5-flash-lite rejects it outright, 3.5-flash takes either. Try it
  and retry without on a 400, rather than keeping a table that goes stale the
  next time Google ships a model.

  Session reuse. A fresh TLS handshake per request costs 100-300 ms, and a
  question that triggers two tool rounds makes four requests. The session is
  passed in rather than owned here so each caller keeps its own connection
  pool and there is no shared lock between the assistant and the summariser.
"""

from __future__ import annotations

import requests


class QuotaExhausted(RuntimeError):
    """429 from the API.

    Carries *which* limit was hit, because the two mean completely different
    things and were being treated identically:

      per minute  ~15 requests on the free tier, and one spoken question can
                  spend five of them (a classify call plus up to four tool
                  rounds, all on the same model). Recovers in under a minute.
      per day     500 per model. Recovers tomorrow.

    Everything treated any 429 as terminal, said "I'm out of API quota for
    today", and switched to the local model permanently -- so a burst of four
    questions in a minute took the assistant offline for the rest of the
    session, and told the user something untrue about why.
    """

    def __init__(self, message: str, per_day: bool = False,
                 retry_after: float = 60.0) -> None:
        super().__init__(message)
        self.per_day = per_day
        self.retry_after = retry_after


def _read_quota(payload: dict) -> tuple[bool, float]:
    """Pull (per_day, retry_after) out of a 429 body.

    Google returns google.rpc.QuotaFailure with a quotaId naming the limit, and
    usually a google.rpc.RetryInfo with how long to wait. Both are best-effort:
    an unparseable body is treated as a per-minute limit, because that is both
    the common case and the safe one -- assuming per-day would take the
    assistant offline for hours over a transient burst.
    """
    per_day, retry_after = False, 0.0
    try:
        for detail in payload.get("error", {}).get("details", []):
            kind = detail.get("@type", "")
            if kind.endswith("QuotaFailure"):
                for violation in detail.get("violations", []):
                    quota_id = (violation.get("quotaId", "")
                                or violation.get("quotaMetric", ""))
                    if "PerDay" in quota_id:
                        per_day = True
            elif kind.endswith("RetryInfo"):
                raw = str(detail.get("retryDelay", "")).rstrip("s")
                try:
                    retry_after = float(raw)
                except ValueError:
                    pass
    except Exception:
        pass

    if not retry_after:
        # A per-minute window is 60 s wide; give it a little room. A per-day
        # limit is not worth retrying often, but is still retried, because the
        # quota resets on Google's clock and not on ours.
        retry_after = 900.0 if per_day else 65.0
    return per_day, retry_after


def post_gemini(session: requests.Session, url: str, key: str, body: dict,
                timeout: float = 25.0) -> dict:
    """POST and return parsed JSON, or raise.

    Mutates nothing the caller passed in: the retry builds its own copy of the
    body, so a caller reusing a template dict does not silently lose its
    thinkingConfig for every subsequent call.
    """
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    r = session.post(url, headers=headers, json=body, timeout=timeout)

    if r.status_code == 400 and "thinkingConfig" in body.get("generationConfig", {}):
        retry = dict(body)
        gen = dict(retry["generationConfig"])
        gen.pop("thinkingConfig", None)
        retry["generationConfig"] = gen
        r = session.post(url, headers=headers, json=retry, timeout=timeout)

    if r.status_code == 429:
        try:
            per_day, retry_after = _read_quota(r.json())
        except Exception:
            per_day, retry_after = False, 65.0
        raise QuotaExhausted(
            "out of Gemini quota for today" if per_day
            else f"sending too fast; retry in {retry_after:.0f}s",
            per_day=per_day, retry_after=retry_after)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def first_candidate_text(payload: dict) -> str:
    """Concatenated text parts of the first candidate, or "".

    Every caller was doing this join by hand with slightly different guards.
    """
    cands = payload.get("candidates") or []
    if not cands:
        return ""
    parts = cands[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)
