# Nod — batch analytics layer

A build brief. Hand this to Claude Code as the spec; it has enough detail to
work phase by phase without re-deciding architecture every session.

---

## The decision this document encodes

The realtime pipeline stays as it is. Airflow is not added to it, and nothing
in `copilot/` gets rewritten. Airflow orchestrates a **second, offline pipeline**
that reads what the app leaves behind.

Why the split:

| | Realtime path | Batch path |
|---|---|---|
| Latency budget | 2–4 seconds end to end | overnight |
| Runs | continuously, while a meeting is live | once a day |
| Unit of work | one utterance | one day of sessions |
| Right tool | threads + queues (already built) | Airflow |
| Terminates? | no — runs until you close the laptop | yes, that's the point |

Airflow's scheduler assumes a DAG starts, runs, and finishes. Task startup alone
costs seconds. A graph that never completes and needs sub-second hops is a
stream, and Airflow does not do streams.

**The payoff:** the batch layer isn't decoration. It is how roadmap item #1 —
"your team's glossary" — actually gets built. Terms that recur across many
meetings are exactly what a nightly aggregation finds, and the output feeds back
into the live app as a lookup table. The batch layer makes the realtime layer
smarter. That's the story to tell.

```
  LIVE (unchanged)                      NIGHTLY (new)
  ────────────────                      ─────────────
  audio ─┐                              session logs
         ├─> memory ─> LLM ─> HUD             │
  screen ┘        │                           ▼
                  │                    Airflow DAG
                  ▼                           │
           session log  ────────────────>     ▼
              (JSONL)                    warehouse (star schema)
                                              │
                                              ▼
                                        glossary.json
                                              │
                  ┌───────────────────────────┘
                  ▼
           live app reads it next morning
```

---

## Phase 0 — the contract (do this first)

Nothing works until the app emits something. Add a `SessionLogger` to
`copilot/` that appends JSONL to `~/.nod/sessions/{session_id}.jsonl`.

**Two modes, because the pitch claims nothing is recorded:**

- `--log metrics` (default): timings, token counts, event kinds, term surface
  forms. No transcript, no suggestion text.
- `--log full` (explicit opt-in): adds transcript segments and suggestion text.
  For your own meetings only.

Keeping the default at `metrics` means the privacy slide stays true. Say on
stage that the analytics layer runs on metadata unless a user opts in.

Event shape:

```json
{"ts": 1721800000.12, "session_id": "2026-07-24T09-00-a1b2", "event": "utterance",
 "duration_sec": 3.4, "word_count": 11, "asr_latency_ms": 820}

{"ts": 1721800004.55, "session_id": "...", "event": "suggestion",
 "kind": "term", "llm_latency_ms": 940, "input_tokens": 1130, "output_tokens": 74,
 "lead_chars": 62, "points_count": 2, "terms": ["SLA", "v2 endpoint"]}

{"ts": 1721800010.00, "session_id": "...", "event": "session_end",
 "duration_sec": 1840, "suggestions": 27, "rate_limited": 1}
```

`terms` is the important field — capitalised acronyms and quoted jargon pulled
out of the suggestion. It's the raw material for the glossary. Extract with a
regex in the app; don't spend an LLM call on it.

**Acceptance:** run a meeting, get a well-formed JSONL file, and `--log metrics`
produces a file containing no meeting content.

---

## Phase 1 — warehouse schema

Postgres. Star schema, since the questions are all "measure X sliced by Y".

**Dimensions**

- `dim_date` — standard date dimension, generated not loaded
- `dim_session` — session_id (natural key), started_at, ended_at, duration_sec,
  log_mode, app_version
- `dim_kind` — summary / term / reply. Tiny, but it keeps the fact table narrow
  and makes the BI layer readable
- `dim_term` — term_sk, term_text, normalised_text, first_seen_date,
  definition (nullable, filled later), is_glossary_candidate

**Facts**

- `fact_utterance` — grain: one transcribed segment
  FKs: date_sk, session_sk · measures: duration_sec, word_count, asr_latency_ms
- `fact_suggestion` — grain: one suggestion shown
  FKs: date_sk, session_sk, kind_sk · measures: llm_latency_ms, input_tokens,
  output_tokens, lead_chars, points_count, est_cost_usd
- `bridge_suggestion_term` — many-to-many, since one suggestion can surface
  several terms. Grain: one suggestion × one term

Surrogate keys everywhere, natural keys kept as degenerate columns.
`bridge_suggestion_term` is the one to get right — it's what makes "which
acronyms does this team actually use" a two-line query.

**Acceptance:** DDL runs clean, FK constraints hold, `dim_date` covers the
current year.

---

## Phase 2 — the DAG

One DAG, `nod_nightly`, `@daily`, `catchup=False` to start.

```
discover_sessions
      │
      ▼
extract_jsonl ──> validate_raw ──> load_staging
                                        │
                        ┌───────────────┼───────────────┐
                        ▼               ▼               ▼
                 build_dim_session  build_dim_term  build_dim_date
                        └───────────────┼───────────────┘
                                        ▼
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
                 fact_utterance                  fact_suggestion
                        └───────────────┬───────────────┘
                                        ▼
                              bridge_suggestion_term
                                        │
                                        ▼
                             compute_glossary_candidates
                                        │
                                        ▼
                                 export_glossary_json
                                        │
                                        ▼
                                    dq_checks
```

Notes that will save you a rewrite:

- **Idempotent tasks.** Delete-then-insert by `date_sk` partition, not blind
  appends. Rerunning yesterday must not double the rows. This is the single
  most common Airflow mistake and reviewers look for it.
- **`validate_raw` is a real task**, not a comment. Row count > 0, required keys
  present, no timestamps in the future. Fail loud.
- **`compute_glossary_candidates`** — a term qualifies when it appears in ≥ 3
  distinct sessions across ≥ 2 distinct days. Tune later.
- **`export_glossary_json`** writes to a path the desktop app reads on startup.
  That's the feedback loop; make it visible in the demo.
- **`dq_checks` last**, and let it fail the DAG. Referential integrity, no
  orphan bridge rows, fact row count within 3σ of the trailing week.

Use TaskFlow (`@task`) rather than classic operators — less boilerplate and the
dependency graph reads better in review.

**Acceptance:** `airflow dags test nod_nightly <date>` runs green twice in a row
with identical row counts the second time.

---

## Phase 3 — Docker

**Do not dockerize the desktop app.** It needs host audio loopback, host screen
capture, and a GUI. On macOS and Windows, Docker Desktop gives you none of the
three. On Linux you'd be forwarding X11 and mounting PulseAudio sockets to
achieve exactly what running it natively already does. It's a day of pain for
negative benefit.

**Do dockerize the analytics stack.** That half is server-side and Docker is
genuinely the right answer:

```
docker-compose.yml
├── postgres-airflow     # Airflow metadata db
├── postgres-warehouse   # the star schema
├── airflow-apiserver    # the UI you're after
├── airflow-scheduler
└── metabase             # optional, but the charts sell it
```

Start from Airflow's official compose file rather than hand-rolling it —
`curl -LfO https://airflow.apache.org/docs/apache-airflow/stable/docker-compose.yaml`
— then strip what you don't need and add the warehouse service. Check which
Airflow major version that pulls; the service names and init steps changed in
Airflow 3.

Mount `~/.nod/sessions` read-only into the scheduler container. The app writes
on the host, Airflow reads in the container, and neither knows about the other.

**Acceptance:** `docker compose up` gives you a working UI, and the DAG can see
session files written by the host app.

---

## How to drive Claude Code on this

It handles this shape of work well — it's mostly scaffolding, SQL, and config.
What makes the difference is how you scope it.

- **One phase per session.** Start a fresh conversation for each. Phase 2 does
  not need Phase 0's discussion in context, and long threads burn tokens fast.
- **Paste this file in as the spec** and say which phase. Ask it to state its
  plan before writing anything.
- **Give it the acceptance criterion as the exit condition** — "done when
  `airflow dags test` runs green twice with identical counts" is checkable in a
  way that "build the DAG" is not.
- **Have it write the DDL before the DAG.** Schema first, always. If the schema
  is wrong the DAG is wasted work.
- Claude Code runs under your Pro plan, so this doesn't touch API credits.

---

## Honest alternatives

- **Dagster** fits this better than Airflow on the merits — asset-based, so the
  lineage graph in the UI is the thing you actually want to look at, and local
  dev is much less painful. If the goal is the prettiest DAG picture, use it.
- **Airflow anyway** if the goal is the line on your CV. It's what job postings
  ask for, and "I know why Airflow was wrong for the streaming half" is a
  stronger interview answer than never having hit the boundary.
- **The node-graph UIs you've seen in AI tools** (LangGraph, n8n, Flowise) are
  runtime dataflow editors, not schedulers. That's a third category, and your
  live pipeline is already one — it just renders as code instead of boxes.
