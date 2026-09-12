# CV service

Turns one clip into the **§6b contract** and nothing else. n8n depends on that
shape alone, so the backend can change freely underneath it.

```
POST /process   {clip_url, match_id, roster, duration_s, fps}  ->  {job_id}
GET  /result/{job_id}                                          ->  §6b
GET  /health
```

## Backends

| `CV_BACKEND` | What it does | Needs |
|---|---|---|
| `mock` | Returns the frozen fixture, re-anchored onto the submitted roster | nothing |
| `lite` | YOLO + ByteTrack + kit-colour teams + homography | GPU, a clip, a calibrated homography |
| `gamestate` | SoccerNet **sn-gamestate** via TrackLab: tracking, re-ID, jersey numbers, pitch localisation | GPU, a clip, the source install |

`mock` is not a stepping stone to be deleted — BUILD_SPEC §11 keeps it as the
demo fallback after the real pipeline works.

## Deploy to Modal

**There is no API key to paste anywhere.** `modal setup` opens a browser, you log
in, and it writes a token to `~/.modal.toml`. Nothing goes into this repo.

```bash
pip install modal
modal setup                                  # browser login, once per machine
```

Then create the secret **before** the first deploy — `modal_app.py` attaches it
by name, so deploying without it fails:

```bash
modal secret create f2t-cv \
  CV_BACKEND=mock \
  HEATMAP_BASE=https://tifynfruqgkbuiyiljfb.supabase.co/storage/v1/object/public/heatmaps
```

Start on `CV_BACKEND=mock`. That proves the deploy, the web endpoint and the n8n
wiring with no GPU and no clip in play. Move to `lite` only once a mock job has
gone end to end.

```bash
cd football-analyst/cv-service
modal deploy modal_app.py
```

Modal prints a URL ending in `/web`. Check it, then point n8n at it:

```bash
curl https://<your-modal-url>/health          # {"ok":true,"backend":"mock"}
```

Put that base URL in **CV_SERVICE_URL** at the top of the `2 Normalize` node —
the only change n8n needs, because the §6b contract is unchanged. Remember to
**publish** the workflow afterwards; saving a draft does not change what the
webhook serves.

### Changing the backend later

```bash
modal secret create f2t-cv --force CV_BACKEND=lite HEATMAP_BASE=...
modal deploy modal_app.py
```

Both are needed: the secret sets the value, the deploy picks it up.

## What the `lite` backend needs before it produces real numbers

**A homography.** Pitch coordinates come from projecting each player's ground
contact point onto a 105 x 68 m plane. Without `PITCH_HOMOGRAPHY` the backend
refuses to guess — every track is dropped rather than emitting coordinates that
look plausible and are wrong.

Calibrate once per camera position (the mount is fixed, so it survives between
matches): pick four points visible in frame whose pitch coordinates you know —
the four corners, or the corners of a penalty box — and solve

```python
import cv2, numpy as np, json
img   = np.float32([[ 210, 980], [1760, 980], [ 520, 470], [1450, 470]])   # pixels
pitch = np.float32([[   0,  68], [ 105,  68], [   0,   0], [ 105,   0]])   # metres
print(json.dumps(cv2.getPerspectiveTransform(img, pitch).tolist()))
```

Paste the result into `PITCH_HOMOGRAPHY`.

## Footage requirements

The camera brand does not matter. These do:

- **Static camera.** No panning, no zooming, no cuts. A phone on a tripod at the
  halfway line qualifies; broadcast footage with replays does not. This is why
  the spec says use the Veo *panorama* export and never the AI follow-cam.
- **Pitch markings visible**, since the homography is solved from them.
- **Resolution.** Players are small in a full-pitch wide shot. 1080p is marginal,
  4K is comfortable.
- **Two clearly different kits**, because team assignment is colour clustering.
- **30–90 seconds**, downsampled to ~5 fps (§2).

## Honest state of this service

- `mock` is **verified** — it reproduces the frozen fixture exactly, including
  the deliberately unresolved track and the team-shape figures.
- `lite` and `gamestate` are **written but never executed.** There is no GPU on
  the machine they were written on. Treat the first Modal run as a debugging
  session, not a demo.
- `gamestate` is the riskiest part, and the reason is not the model: sn-gamestate
  is built around the SoccerNet-GSR dataset layout rather than loose video, so
  `_gamestate()` writes the frames into that layout and drives TrackLab over it.
  That adapter is where this will need the most attention.
- Jersey-number OCR only exists in the `gamestate` backend. `lite` anchors
  identity from the roster and reports a lower `id_confidence` to say so — §7a
  is explicit that OCR alone is not trustworthy on wide footage.
