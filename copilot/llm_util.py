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
    """429 from the API. Separate from other failures because it is the one
    the user can act on -- it means wait, not retry."""


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
        raise QuotaExhausted("out of Gemini quota")
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
