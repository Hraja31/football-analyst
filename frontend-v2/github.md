repo: shaneesilva/football-analyst
branch: main

## Last sync
date: 2026-09-13T02:03:52Z

### Updated in this project
- Built a new coach-facing front end covering landing, upload, processing, report, pitch map, players and settings.
- Video intake now accepts a dropped file or a pasted panorama link in one step.
- Processing screen uses a pitch-shaped progress bar and collects manual stats while the run completes.
- Tracking confidence and degraded-run states are surfaced rather than hidden.

## Screen map
| Screen | Built from |
| --- | --- |
| Landing | README.md |
| Upload | frontend/index.html, frontend/app.js |
| Processing | frontend/app.js, n8n/WORKFLOW_OVERVIEW.md |
| Match report | sample-data/report.sample.json, sample-data/roster.json |
| Pitch map | sample-data/cv_result.json, sample-data/heatmaps/*.svg |
| Players and player report | sample-data/report.sample.json, sample-data/cv_result.json |
| Matches | db/schema.sql |
| Settings | README.md, db/schema.sql |
