# Progress log — Footage → Tactics

Demo: **2026-09-13**. Track 1 (Best Overall / End-to-End Integration).
Newest entry first. Kept for the C3 progress-update criterion and as the build's
running state.

---

## Status at a glance

| Rung | Scope | State |
|---|---|---|
| **1** | Working demo on mock data: form → merge → Claude → report → frontend | ✅ **running live, ~100 s end to end** |
| **2** | Supabase persistence + verified CV failure branch | 🟡 project live, schema + bucket in place; failure branch verified offline, not yet live |
| **3** | Real CV pipeline on GPU | 🟡 `cv-service/` written and deployable to Modal; never executed on a GPU |

| Component | State | Notes |
|---|---|---|
| Data contracts §6a / §6b / §10c | ✅ | Frozen and cross-validated |
| Fixtures + heatmap generator | ✅ | `scripts/generate_fixtures.py`, seeded and re-runnable |
| Coach frontend | ✅ | Form + report view, verified in-browser |
| DB schema | ✅ | Run against the live project; `heatmaps` bucket public, 11 SVGs uploaded |
| README + workflow overview | ✅ | B5 pass/fail gate covered |
| n8n mock CV workflow | ✅ | 6 nodes, reproduces the frozen fixture exactly — verified live over HTTP |
| n8n main pipeline | ✅ | 34 nodes, **verified live end to end** |
| Frontend pointed at the live webhooks | ✅ | |
| Supabase project | ✅ | `tifynfruqgkbuiyiljfb`, credential wired to 7 nodes |
| Anthropic key | ✅ | In the n8n credential store; Sonnet 5 |
| Playing-style tactics | ✅ | Team call returns press height, line, width, tempo, out-of-possession |
| `cv-service/` for Modal | 🟡 | `mock` verified; `lite` and `gamestate` written, never executed |
| Failure branch verified **live** | ✅ | Mock CV deactivated mid-flight: `degraded: true`, 11 players, report in 47 s |

---

## 2026-09-13 — demo day: the pipeline runs end to end

**Working, verified live on `shaneesilva.app.n8n.cloud`**

A full run completes in **~100 seconds**: form → mock CV → merge → Claude →
report, all eleven players analysed, served as JSON and as a standalone HTML page
with heatmaps loading from Supabase.

The analysis is grounded, not generic. It picked up Doyle's 28.6% tackle success
and tied it to the coach's note about conceding down the left, spotted Osei's 0.89
attacking-third share as isolation, and wrote a 2v2 overlap drill aimed at exactly
that flank.

**What had to change, and why**

| Problem | Fix |
|---|---|
| One call for all 11 players: ~5,000 tokens, ~285 s, JSON that would not parse — three runs running | Split into one small team call plus 11 per-player calls at Batch Size 5 |
| The Structured Output Parser rejected every reply and took the run down with it | Removed. Node 11 parses the text itself and tolerates fences, preamble and trailing prose |
| A model closed a nested object early, leaving a stray `}`. The prefix was valid JSON, so the parser silently dropped `tactics` and half the team block | Node 11 now collects every reading that parses and keeps the richest one |
| `6b` read `$input`, which is the team call's output, not the profile | Reads `$('6 Build profile')` by name |
| Team reply truncated at exactly 2,000 tokens | One model node serves both calls, so the cap has to fit the larger one — 4,000 |

**Two n8n behaviours that cost the most time**

1. **Import appends, it never replaces.** Importing over the existing workflow gave
   75 nodes with duplicate names and conflicting webhook paths. Clear the canvas first.
2. **A draft is not live.** MCP/API edits update the draft; the webhook serves the
   published version until you publish. Two correct fixes appeared to do nothing.

**Also built today**

- `cv-service/` — FastAPI + Modal deploy target, three backends (`mock`, `lite`,
  `gamestate`). `mock` verified; `lite` and `gamestate` written but never executed,
  because there is no GPU here. First Modal run is a debugging session, not a demo.
- Playing-style tactics: the team call now returns press height, defensive line,
  width, tempo and out-of-possession shape.

**Still open**

1. `cv-service` has never run on a GPU. No clip, no calibrated homography.
2. sn-gamestate's dataset adapter is the riskiest unknown in that service.
3. ~~Frontend poll ceiling~~ — `POLL_MAX` raised to 72 (~180 s) against a ~75 s run.

---

## 2026-09-12 — day 1, later: the two workflows

**Done**

- Built `n8n/mock_cv_workflow.json` (6 nodes). Two webhooks — `cv-mock/process`
  and `cv-mock/result` — with the §6b payload built on the way in and parked in
  workflow static data, so the result endpoint can answer `processing` on poll 1
  and `done` on poll 2. It re-anchors the fixture onto whatever roster it is
  handed, so it works for any club, and it recomputes team shape with the same
  definitions as `generate_fixtures.py` — it reproduces 19.4 / 46.9 / 33.9 m
  exactly. `?fail=1` forces the failure path without deactivating anything.
- Built `n8n/tool1_workflow.json` (33 nodes). All thirteen spec nodes plus the
  read endpoints. Notes below on the parts that needed a decision.
- Added the `v_match_report` view to `db/schema.sql` so node 12b needs one GET
  rather than three.
- Pointed `frontend/app.js` at the live webhooks and added a link to the
  shareable HTML report.

**Decisions**

| Decision | Why |
|---|---|
| The Merge node joins CV against roster+manual, not all three | Roster and manual stats arrive in the same form POST; joining them needs no Merge. The Merge is kept for the join that actually spans two systems |
| `5b CV rows` emits a sentinel item when there is no CV data | A Merge input that gets zero items leaves the branch with nothing to execute and the run never reaches Claude — the exact hang 4b exists to prevent |
| Supabase writes hang off a side branch, all set to continue on error | Rung 2 is still blocked on the account. A database that does not exist must not be able to stop a coach getting a report |
| Report endpoint reads workflow static data first, Supabase second | The demo cannot depend on a project that may not exist by Sunday. The DB is the durable copy, not the critical path |
| Frontend POSTs `text/plain` | It is one of the three content types that skip the CORS preflight; this n8n webhook node version has no `allowedOrigins` option and does not answer `OPTIONS`. The body is still JSON |
| Sonnet 5, not Opus 5, on node 8 | An 11-player report is ~5 k output tokens. The frontend gives up after 100 s; Opus would make that tight. Documented on the node as a one-field swap |
| Auto-Fix on the output parser, with its own model connection | The spec asks for one retry on invalid JSON. Auto-Fix makes a second LLM call, so the parser needs its own `ai_languageModel` input — easy to miss, and it fails at runtime if you do |
| Spatial fields absent on the degraded path, never zeroed | `distance_m: 0` reads to the analyst as "he did not move". That is a different and false claim |

**Verified, offline**

n8n was unreachable over MCP this session (the server rejected the configured
Authorization header), so everything was checked without it:

- every Code node run in Node against the frozen fixtures, wired in canvas order
  with the Merge node's Enrich-Input-1 semantics reimplemented. Happy path and
  degraded path both produce a complete §10b prompt and a valid §10c report;
- node 11's repair logic tested against six malformed model outputs — short XI,
  duplicate ids, unknown ids, missing players, out-of-range and non-numeric
  ratings, missing `team`, missing SWOT arrays. All six give eleven unique known
  names in the XI and one card per squad player;
- rendered HTML parsed for well-formedness: 11 cards and 11 heatmaps on the happy
  path, 0 heatmaps and a banner on the degraded path;
- every node config validated against the n8n node schemas; the connection graph
  linted for unknown references, orphans, unbalanced expressions and unreachable
  nodes; `db/schema.sql` parsed with the Postgres dialect.

**Still unproven — what the first live run has to confirm**

1. The Wait node's loop-back timing on n8n Cloud.
2. The Anthropic credential and the `claude-sonnet-5` model id in that node's list.
3. The Supabase upserts and the `v_match_report` view, against a real project.
4. CORS end to end from `localhost:8787` to the n8n Cloud webhooks.

**Next, in order**

1. Export `N8N_MCP_TOKEN` and restart, or import both JSON files by hand.
2. Activate the mock workflow first, then the main one.
3. Add the Anthropic credential; run once with `sample-data/roster.json` via curl.
4. Hand-check nodes 4 and 4b in the execution log — §14 flags these as the two an
   LLM most often gets subtly wrong.
5. Run the failure test three ways (see README).
6. Supabase if time allows; drop it without regret if it threatens the critical path.
7. Rehearse twice end to end; capture a 60 s screen recording as a fallback.

---

## 2026-09-12 — day 1

**Done**

- Connected n8n over MCP. Discovered the token in `.env` was not a Public API
  key: it 401s against `/api/v1/` but authenticates against
  `/mcp-server/http`, which is n8n's own built-in MCP server (`v1.2.0`).
  Configured as an HTTP MCP server with a Bearer header in `../.mcp.json`,
  with the token read from `${N8N_MCP_TOKEN}` rather than committed.
  Added the community `n8n-mcp` package alongside it for node-schema validation.
- Froze the two data contracts (§6a, §6b) plus the §10c report schema. This is
  what allowed the frontend and fixtures to be built before any node exists.
- Built `scripts/generate_fixtures.py`: per-position movement models → sampled
  positions → density grid → SVG heatmap, and `cv_result.json` derived from the
  same samples so the spatial figures and the images agree.
- Generated 11 heatmaps + `cv_result.json`, including one deliberately
  unresolved track (`player_id: null`, `id_confidence` 0.31) to exercise the
  §7a honesty path.
- Wrote `sample-data/report.sample.json` — a full 11-player analyst report used
  for offline frontend work and as the live demo fallback.
- Built the coach frontend: form with editable manual stats, progress view,
  and a report view with best XI on a pitch, team SWOT, and per-player cards
  showing heatmap, rating, SWOT and training plan.
- Verified the frontend in headless Chrome. Fixed one real defect: best-XI
  markers plotted from CV average positions collided in midfield, making shirt
  numbers unreadable. Added a relaxation pass to push them apart.
- Wrote `README.md` and `n8n/WORKFLOW_OVERVIEW.md`.

**Decisions**

| Decision | Why |
|---|---|
| Mock CV service is a **second n8n workflow**, not FastAPI on localhost | n8n Cloud cannot reach localhost, and a tunnel can drop mid-demo. Also gives a real multi-workflow story for B6. |
| Mock returns `processing` then `done` | Makes the node-4 polling loop genuinely execute instead of short-circuiting |
| Frontend decoupled behind two webhooks | Lets it be built before the flow exists, and swapped freely |
| Heatmaps as SVG, not PNG | numpy/matplotlib unavailable locally; a venv is time we don't have. `heatmap_url` is format-agnostic |
| Distances ~60–190 m, not the spec's 812 m | §6b's sample implies 48 km/h sustained over 60 s. Football-literate judges would notice |
| Rung 3 dropped to roadmap | 24 h and no GPU host. §14 and the B6 wording both say a simpler flow that fully works beats an ambitious one that partially fails |

**Blockers**

1. **Anthropic API key** — nothing substitutes for this; node 8 is the product.
2. **Supabase project** — needed for Rung 2 persistence and public heatmap URLs.
   Droppable if it threatens the critical path.
3. **MCP tools need a Claude Code restart** with `N8N_MCP_TOKEN` exported before
   the n8n workflows can be built.

**Next, in order** *(done — see the entry above)*

1. ~~Restart with the token exported; approve both MCP servers.~~ Still 401ing;
   worked around by building the workflow JSON offline instead.
2. ~~Build the mock CV workflow.~~
3. ~~Build the main pipeline.~~
4. ~~Point the frontend at the live webhooks.~~
