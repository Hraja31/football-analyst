# Footage → Tactics

Turns one Veo panoramic match clip into a **tracked per-player heatmap** and an
**AI analyst report** — per-player SWOT, team SWOT, recommended formation and
best XI, a training plan per player, and a rating.

**For:** the semi-pro / local club coach who already films on a Veo camera but
has no analyst, no budget, and no time.

**Hackathon track:** Best Overall / End-to-End Integration.

**Demo video:** [`demo/footballanalyst.mp4`](demo/footballanalyst.mp4) (2:19, walks through a live run from submission to report)

---

## Architecture

```
Coach form (clip URL + manual stats + roster)
        │
        ▼
   n8n workflow  ──HTTP──►  CV service  ──JSON+SVG──►  n8n
   (orchestrator)            (mock CV workflow now;         │
        │                     Modal GPU at Rung 3)          ▼
        │                                          Merge CV + manual + roster
        ▼                                                   │
   Supabase (per-club DB + heatmap storage) ◄────────── build profile
        │                                                   │
        ▼                                                   ▼
   Report JSON ◄──────────── Claude analyst (SWOT / XI / formation / plan)
        │
        ▼
   Custom frontend (frontend/index.html)
```

n8n orchestrates; the CV is a single external HTTP call. **If the CV call fails,
the pipeline degrades to manual-only data and still completes.** That branch is
required, not a nicety — see `n8n/WORKFLOW_OVERVIEW.md`.

The coach's browser never waits on the analysis: the form is answered with a
`match_id` in under a second (workflow node 13) and the frontend polls a second
webhook until the report is ready.

---

## What is built

| Component | State |
|---|---|
| Data contracts (§6a, §6b, §10c) | ✅ frozen, with validated fixtures |
| Heatmap generation | ✅ 11 per-player pitch heatmaps, generated from positional models |
| Coach frontend | ✅ form + report view, verified in-browser |
| Report fixture | ✅ full 11-player analysis for offline demo |
| DB schema | ✅ `db/schema.sql`, ready to run |
| n8n main pipeline | ✅ `n8n/tool1_workflow.json`, 38 nodes — see `n8n/WORKFLOW_OVERVIEW.md` |
| n8n mock CV service | ✅ `n8n/mock_cv_workflow.json`, honours the §6b contract |
| Real CV service | ✗ Rung 3, deliberately out of scope for this build — see below |

---

## Run it

### Frontend only (no backend, no credentials)

```bash
cd football-analyst
python3 -m http.server 8787
open "http://localhost:8787/frontend/index.html?demo=1"
```

`?demo=1` loads the bundled sample report from `sample-data/`. This path works
with no network, no n8n and no API keys — it is the demo fallback.

### Connected to n8n

1. Import both files in `n8n/` (**Workflows → Import from File**). Activate
   `mock_cv_workflow.json` first — the main pipeline calls it over HTTP, so it
   has to be live, not just saved.
2. Add the **Anthropic** credential to the `Anthropic Chat Model` node. This is
   the one credential the demo cannot run without.
3. Optional but recommended — Supabase:
   - run `db/schema.sql` in the SQL editor (tables, the `heatmaps` bucket, and
     the `v_match_report` view the report endpoint reads);
   - add the **Supabase account** credential to the six HTTP nodes that use it
     (`7a`–`7d`, `10a`, `10b`, `12b`);
   - put your project URL in `SUPABASE_URL` at the top of the `2 Normalize` Code
     node **and** in `12 Lookup report`;
   - set `CV_SERVICE_URL` in `2 Normalize` to your CV backend — the Modal
     service (`cv-service/`) or the mock workflow (`https://<host>/webhook/cv-mock`).
     Both answer `GET /result?job_id=`, so switching is that one line;
   - upload `sample-data/heatmaps/*.svg` to the public `heatmaps` bucket and
     regenerate the fixture so the heatmaps have real URLs:
     ```bash
     python3 scripts/generate_fixtures.py \
       --base-url https://<project>.supabase.co/storage/v1/object/public/heatmaps
     ```
   Skip all of this and the pipeline still runs end to end — it just holds the
   report in n8n rather than in a database.
4. Activate `tool1_workflow.json`, then check the three production webhook URLs
   match what `frontend/app.js` has at the top (`match-submit`, `report`,
   `report-html`). If your instance host differs, change them there.
5. Serve the frontend and submit the form.

A quick check without the browser, once both workflows are active:

```bash
curl -s -X POST https://<host>/webhook/match-submit \
  -H 'Content-Type: application/json' \
  --data @sample-data/roster.json
# -> {"match_id":"m_001","status":"accepted","report_url":"/webhook/report?match_id=m_001"}

sleep 45
curl -s 'https://<host>/webhook/report?match_id=m_001' | head -c 400
open  'https://<host>/webhook/report-html?match_id=m_001'
```

### Failure test (the one judges should see)

Deactivate `mock_cv_workflow` in n8n and submit again. The run completes, the
report still arrives, spatial fields are empty, and the frontend shows a banner
explaining that CV did not complete. Nothing hangs and nothing errors out.

Four variants, all of which land on the same branch:

| What you break | Where it is caught |
|---|---|
| Deactivate the mock workflow | node 3 gets no `job_id` → `4 CV job accepted?` routes straight to 4b |
| Append `?fail=1` to the result URL in `2 Normalize` | the mock returns `status: failed` → 4b on the first poll |
| Point `CV_SERVICE_URL` at a black hole | node 3 times out after 20 s with no `job_id` → straight to 4b |
| A CV service that accepts the job and never finishes | polls for ~10 min (60 × 10 s), then 4b on timeout |

---

## Environment

Copy `.env.example` to `.env` and fill in. Secrets are never committed and never
hard-coded into workflow JSON — node credentials live in the n8n credential store.

`N8N_MCP_TOKEN` is not part of this file: it is the Claude Code MCP connection,
referenced by `../.mcp.json` and exported in your shell.

---

## Honest limitations

Worth stating plainly, because they are design decisions rather than oversights:

- **Player identity is anchored from the roster, not read from shirts.** Teammates
  wear identical kits, so the jersey number is the only individual cue and it is
  legible in a fraction of wide-angle frames. Identity is robust over a 30–90 s
  clip and degrades over a full match. Tracks that cannot be resolved carry
  `player_id: null` and a low `id_confidence`, and are used for team shape only —
  the frontend shows that confidence rather than hiding it.
- **Event detection is out of scope.** Passes, tackles, dribbles and headers are
  coach-entered, not inferred from video. Inferring them unreliably would make
  the whole report untrustworthy.
- **Clips, not full matches.** 30–90 s at ~5 fps.
- **The CV service is currently mocked.** The §6b contract is frozen, so the real
  pipeline (`ultralytics` + ByteTrack + `roboflow/sports` homography + `mplsoccer`,
  deployed to Modal) drops in behind the same HTTP interface with no change to n8n.
- **Heatmaps in this build are generated from positional models,** not from real
  tracking. They demonstrate the contract and the report; they are not claimed as
  real CV output.

## Layout

```
football-analyst/
├─ n8n/          tool1_workflow.json, mock_cv_workflow.json, WORKFLOW_OVERVIEW.md
├─ db/           schema.sql
├─ frontend/     index.html, app.js, styles.css
├─ sample-data/  roster.json, cv_result.json, report.sample.json, heatmaps/
├─ scripts/      generate_fixtures.py
└─ .env.example
```
