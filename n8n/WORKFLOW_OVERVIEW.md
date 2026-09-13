# Workflow overview — Footage → Tactics (Tool 1)

**Status:** both workflows are built and importable. Every Code node in them has
been executed against the frozen fixtures — happy path and CV-failure path —
before import; see "How this was verified" at the end.

Two workflows run on `shaneesilva.app.n8n.cloud`:

| Workflow | File | Role |
|---|---|---|
| `tool1_workflow.json` | main pipeline, 38 nodes | form → CV → merge → Claude → store → serve |
| `mock_cv_workflow.json` | mock CV service, 6 nodes | stands in for the GPU service behind the same HTTP contract |

---

## Why the CV service is an n8n workflow

n8n Cloud cannot reach `localhost`, so a FastAPI mock on a laptop is unreachable.
Rather than depend on a tunnel that can drop mid-demo, the mock CV service is a
second n8n workflow with a Webhook trigger. It honours the exact §6b contract, so
swapping in a real GPU endpoint at Rung 3 is a single URL change in node 2 of the
main workflow.

It returns `status: "processing"` on the first poll and `status: "done"` on the
second, so the polling loop in node 4 genuinely runs rather than short-circuiting.

---

## Main pipeline, node by node

Node numbers follow BUILD_SPEC §9. Where the built flow splits a spec row across
two nodes, both are listed against that number.

| # | Node | n8n type | Input → Output |
|---|---|---|---|
| 1 | `1 Coach form` | Webhook (POST `/match-submit`) | Frontend JSON → `roster.json` shape (§6a) |
| 2 | `2 Normalize` | Code | Raw body → `match_id`, `club_id`, `players[]`, `manual_stats[]`, `coach_notes`, `playing_style`, plus the two endpoint constants |
| 13 | `13 Respond to coach` | Respond to Webhook | Returns `{ match_id, status, report_url }` immediately, then execution continues |
| 3 | `3 Start CV job` | HTTP Request | `POST {CV_SERVICE_URL}/process` `{clip_url, roster}` → `{job_id}` |
| 4 | `4 CV job accepted?` | If | `job_id` present? No → straight to 4b, without polling a service that isn't there |
| 4 | `4 Wait for CV` → `4 Get CV result` → `4 CV done?` → `4 Poll again?` | Wait → HTTP Request → If → If | `GET /result?job_id=` until `status=done`; max 60 polls, 10 s apart (~10 min). `failed` exits on the next check |
| **4b** | `4b CV unavailable` | Set | On `failed`, unreachable or timeout: `degraded=true`, `cv_error` set, CV fields left absent, **execution continues** |
| 5 | `5a Roster rows` / `5b CV rows` / `5 Merge CV + manual` | Code / Code / Merge | Combine by `player_id`, Enrich Input 1 → one row per squad player |
| 6 | `6 Build profile` | Code | Derived metrics (conversion, accuracy, tackle success, save rate) + team aggregate → the §10b user message |
| 7 | `7a`–`7d` Upsert club / players / match / player stats | HTTP Request ×4 (Supabase) | PostgREST upsert (`Prefer: resolution=merge-duplicates`) |
| 8b | `8b Ask Claude - team` | Basic LLM Chain + Anthropic Chat Model | One small call: formation, best XI, team SWOT, and the tactics the chosen style implies |
| 6b | `6b Player prompts` | Code | Fans the profile out into one item per player, each with its own short prompt |
| 8a | `8a Ask Claude - players` | Basic LLM Chain, Batch Size 5 | Eleven small calls, five at a time |
| 9 | *(removed)* | — | The Structured Output Parser is gone. §10c is now enforced in code, in node 11 — see "Why the parser was removed" |
| 10 | `10a Save player reports` / `10b Save team report` | HTTP Request ×2 (Supabase) | Writes `reports` + `team_reports` |
| 11 | `11 Format report` | Code | Parses both calls, repairs the XI, caches the report, renders self-contained HTML |
| 11b | `11b Report page request` → `11b Render page` → `11b Respond HTML` | Webhook (GET `/report-html`) → Code → Respond | A shareable one-page report |
| 12 | `12 Report request` → `12 Lookup report` → `12 Report ready?` → `12 Respond report` | Webhook (GET `/report`) → Code → If → Respond | `?match_id=` → report JSON + CV data, CORS `*` |
| 12b | `12b Fetch from Supabase` → `12b Assemble from DB` | HTTP Request → Code | Cache miss → the `v_match_report` view |

### Connections

```
1 → 2 → 13 → 3 → 4(accepted?) ──yes──► Wait ──► Get result ──► done? ──yes──► 5b ──► 5 ──► 6
                     │                    ▲                       │                        │
                     │                    └──yes── Poll again? ◄──no                       │
                     └──no──────────────► 4b ──────────────────────┘ (no)                  │
                                                                                           │
2 ─────────────────────────────────────────────────► 5a ──► 5 (input 1)                    │
                                                                                           ▼
      6 → 7a → 7b → 7c → 7d → 8b → 6b → 8a → 11 → 10a → 10b
          └─ Supabase, in series ─┘   └── one team call, then 11 player calls ──┘

12  (separate trigger) → static-data cache, else Supabase view → responds
11b (separate trigger) → static-data cache → responds HTML
```

Three things in that diagram are deliberate and worth pointing at:

**Node 13 sits between 2 and 3.** The coach's browser gets `{ match_id }` in
well under a second and starts polling; the analysis then runs for as long as it
needs without an HTTP connection held open across it.

**Node 5 has two inputs from two different places.** Input 1 comes straight off
node 2, input 2 off whichever CV branch won. The Merge is set to *Combine → by
matching fields → `player_id` → Enrich Input 1*, so every squad player survives
even when input 2 carries nothing but a sentinel.

**Supabase runs in series, before Claude.** Nodes 7a–7d used to hang off node 6
as a side branch. n8n's v1 engine runs one branch to completion before starting
the next, ordered by canvas position, and 8b sat above 7a — so the whole Claude
branch, including `10a`/`10b`, ran before `7c` had created the match row. Every
report insert hit `reports_match_id_fkey` (409), continue-on-error swallowed it,
and no report ever reached Supabase; the report endpoint had been serving n8n's
in-memory copy all along. The order now lives in the wiring rather than in the
layout: `6 → 7a → 7b → 7c → 7d → 8b`, so club, players and match exist before
anything references them.

Resilience is unchanged. All four writes continue on error *and* always output
data, so a database that is down, misconfigured or not yet created still hands
an item to 8b and cannot stop a coach getting a report. `8b` reads `team_prompt`
from `6 Build profile` by name, since its direct input is now 7d's empty
response. A write failure is still silent in the report itself — check the
execution log, or `v_match_report`, to confirm a match was stored.

### Why the parser was removed

The first build asked Claude for the whole eleven-player report in one call and
enforced §10c with a Structured Output Parser. Three live runs failed the same
way: **~5,000 output tokens over ~285 seconds, and JSON the parser rejected.**
Auto-Fix made a second call and failed too. Even had it parsed, 285 s is unusable
— the frontend gives up at 100.

So the call was split and the parsing moved into code:

- one small call for the team picture, then one per player at Batch Size 5;
- node 11 extracts JSON from each reply itself, tolerating code fences, preamble
  and trailing prose;
- a reply that cannot be salvaged costs **one player card**, not the run.

A live run now completes in **~100 seconds** with all eleven players analysed.

One case worth recording, because it is not obvious: a model can close a nested
object a key early and leave a stray `}` mid-reply. The prefix ending at that
brace is *perfectly valid JSON*, so a parser that stops at the first balanced
brace silently drops everything after it — in our case `position_weaknesses`,
`playing_style_note` and the whole `tactics` block, with no error anywhere.
Node 11 now collects every reading that parses and keeps the richest one.

### The failure branch (node 4b) — the point of the design

Every node that talks to the outside world (`3`, `4 Get CV result`, `7a`–`7d`,
`10a`, `10b`, `12b`) is set to **continue on error**, so a network failure
produces an item rather than a stopped execution.

If the CV service is dead, refuses the job, reports `failed`, or never finishes
within the polling window (~10 minutes), the run lands on `4b CV unavailable`, and:

- `degraded: true` and a human-readable `cv_error` are set;
- `5b CV rows` emits a sentinel, so the Merge branch cannot stall;
- spatial fields are *absent*, never zeroed — a `distance_m` of `0` would read to
  the analyst as "he did not move", which is a different and false claim;
- the §10b prompt tells Claude in as many words that there is no spatial data and
  that it must not infer positioning or workrate;
- the report is written, stored and served as normal, and the frontend shows a
  banner explaining what is missing.

Verified by disabling the mock CV workflow and re-running — see README
"Failure test".

### Credentials used

| Node | Credential | Where it lives |
|---|---|---|
| 7a–7d, 10a, 10b, 12b | Supabase account (`supabaseApi`) | n8n credential store |
| Anthropic Chat Model | `ANTHROPIC_API_KEY` | n8n credential store |
| 3, 4 | none (mock); bearer token at Rung 3 | — |

No secret is stored in the workflow JSON or in this repo. The two non-secret
endpoint constants (`CV_SERVICE_URL`, `SUPABASE_URL`) are at the top of the
`2 Normalize` Code node, and `SUPABASE_URL` is repeated in `12 Lookup report`
because that branch has its own trigger and cannot read node 2's output.

### Webhook URLs

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/match-submit` | the coach form |
| GET | `/webhook/report?match_id=` | report JSON for the frontend |
| GET | `/webhook/report-html?match_id=` | the shareable report page |
| POST | `/webhook/cv-mock/process` | mock CV, start a job |
| GET | `/webhook/cv-mock/result?job_id=` | mock CV, §6b payload |

The frontend POSTs with `Content-Type: text/plain` on purpose: it is one of the
three types a browser sends without a CORS preflight, and an n8n Cloud webhook
does not answer the `OPTIONS` request that `application/json` would trigger. The
body is still JSON, and node 2 accepts either a string or an object.

---

## Data contracts

Frozen before any node was built, which is what let the frontend and the
fixtures be built in parallel with the flow:

- **§6a `roster.json`** — form output. See `sample-data/roster.json`.
- **§6b `cv_result.json`** — the only thing n8n depends on from the CV service.
  See `sample-data/cv_result.json`.
- **§10c report schema** — enforced by node 9. See `sample-data/report.sample.json`.

---

## How this was verified before import

n8n was unreachable over MCP while these were built (the token authenticates
against `/mcp-server/http` but the server rejected the configured header this
session), so the workflows were verified offline instead:

- every Code node was run in Node against the frozen fixtures, wired in the same
  order as the canvas, with the Merge node's *Enrich Input 1* semantics
  reimplemented — both the happy path and the degraded path produce a complete
  §10b prompt and a valid §10c report;
- node 11's repair logic was tested against six malformed model outputs: a short
  best XI, duplicate and unknown player ids, missing players, out-of-range and
  non-numeric ratings, a missing `team` object, and missing SWOT arrays. All six
  yield eleven unique known names in the XI and one card per squad player;
- the rendered HTML was parsed for well-formedness (11 cards, 11 heatmaps on the
  happy path, 0 and a banner on the degraded path);
- every node configuration was validated against the n8n node schemas, and the
  connection graph was linted for unknown node references, orphans, unbalanced
  expressions and unreachable nodes.

### Confirmed by live runs on 2026-09-13

Everything below was checked against real executions on `shaneesilva.app.n8n.cloud`,
not offline:

- **The polling loop works.** `4 Wait for CV` and `4 Get CV result` each ran twice
  — `processing` on the first poll, `done` on the second — and `4 Poll again?` fired
  once. §14 flagged this as the node most often got subtly wrong; it is correct.
- **The Merge survives the Wait.** `5 Merge CV + manual` receives input 1 from a
  branch that runs *before* the Wait node and input 2 from after it. That was the
  failure I most expected; it executes cleanly.
- **Workflow static data persists between executions**, which the mock CV service
  and the report cache both depend on.
- **The mock CV service** reproduces the frozen fixture exactly over HTTP,
  including `?fail=1`.
- **End to end in ~100 seconds**, eleven players, no gaps.

Two n8n behaviours worth writing down, because both cost real time:

1. **Import always appends.** "Import from File" adds to whatever is on the canvas
   — it never replaces. Importing over an existing workflow produced 75 nodes with
   duplicate names and conflicting webhook paths. Clear the canvas first.
2. **A draft is not live.** Editing over the API or MCP updates the *draft*; the
   webhook keeps serving the published version until you publish. Two fixes
   appeared to do nothing for exactly this reason.
