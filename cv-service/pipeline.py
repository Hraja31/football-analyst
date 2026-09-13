"""Clip -> §6b contract.

Three backends behind one function, so n8n never learns which one ran:

    mock       the frozen fixture. No GPU, no clip.
    lite       YOLO + ByteTrack + kit clustering + homography.
    gamestate  SoccerNet sn-gamestate via TrackLab: detection, tracking, re-ID,
               jersey-number recognition and pitch localisation in one pass.

Pitch coordinates are always 105 x 68 m, x along the length, attacking left to
right, because that is what §6b says and what the Claude prompt explains.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PITCH_L, PITCH_W = 105.0, 68.0
HEATMAP_BASE = os.environ.get("HEATMAP_BASE", "")


# ──────────────────────────────────────────────────────────────── helpers ────
# A 90 s clip is tens to a few hundred MB even at 4K. The cap is what stops a
# pasted full-match export, or a link that never ends, filling the container disk.
MAX_CLIP_MB = float(os.environ.get("MAX_CLIP_MB", "1024"))


def _download(url: str, dest: Path) -> Path:
    """Fetch the clip, refusing anything that is plainly not a video file.

    The common wrong link is a page, not a file — a YouTube, Drive or Veo viewer
    URL. Left alone that surfaces three steps later as "ffmpeg produced no
    frames", so it is named here instead.
    """
    import requests  # noqa: PLC0415

    limit = int(MAX_CLIP_MB * 1024 * 1024)
    with requests.get(url, stream=True, timeout=(30, 300)) as r:
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype.startswith("text/") or ctype in ("application/json", "application/xhtml+xml"):
            raise ValueError(
                f"clip_url returned {ctype}, not a video. It must be a direct link to the "
                "video file, not a YouTube, Drive or Veo viewer page.")
        declared = r.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ValueError(f"clip is {int(declared) / 1048576:.0f} MB; the limit is {MAX_CLIP_MB:.0f} MB")

        written = 0
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                written += len(chunk)
                # Checked while streaming too: Content-Length can be absent or wrong.
                if written > limit:
                    raise ValueError(f"clip exceeds the {MAX_CLIP_MB:.0f} MB limit")
                fh.write(chunk)
    return dest


def _extract_frames(clip: Path, out_dir: Path, fps: int, duration_s: float) -> list[Path]:
    """Downsample with ffmpeg (§7 step 1). Frames, not video, from here on."""
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip),
         "-t", str(duration_s), "-vf", f"fps={fps}",
         str(out_dir / "%06d.jpg")],
        check=True,
    )
    return sorted(out_dir.glob("*.jpg"))


# Per-frame pitch calibration on a moving camera is noisy, and not gently: on a
# 30 s tactical-cam clip a third of raw frame-to-frame steps implied speeds no
# human reaches, and the worst were calibration failures putting a player tens of
# metres away for one frame. Summed as distance that produced 465 m in 23 s. The
# cleaning below was tuned on that clip (see README, "Tracking noise"):
OUTLIER_M = 4.0        # drop a point further than this from its local median
SMOOTH_S = 0.9         # half-width of the median smoothing window, seconds
MAX_STEP_MPS = 10.0    # ~36 km/h, just above an elite sprint; faster is not movement
# Less believable movement than this measures nothing. Such a track still has a
# position and a heatmap, but distance and speed are null — never 0, which the
# analyst would read as "did not move" (the same rule node 4b follows).
MIN_MOVEMENT_S = 2.0


def _clean_track(pts: list[tuple[float, float, float]], fps: int) -> list[tuple[float, float, float]]:
    """Reject one-frame calibration spikes, then median-smooth what is left.

    A median, not a mean: a mean would smear a 40 m spike across its
    neighbours instead of discarding it.
    """
    import statistics  # noqa: PLC0415

    half = max(1, int(SMOOTH_S * fps))
    pts = sorted(pts)

    def around(seq, i):
        return seq[max(0, i - half): i + half + 1]

    kept = []
    for i, (t, x, y) in enumerate(pts):
        nb = around(pts, i)
        if math.hypot(x - statistics.median(p[1] for p in nb),
                      y - statistics.median(p[2] for p in nb)) <= OUTLIER_M:
            kept.append((t, x, y))
    return [(t, statistics.median(p[1] for p in around(kept, i)),
             statistics.median(p[2] for p in around(kept, i)))
            for i, (t, _, _) in enumerate(kept)]


def _aggregate(tracks: dict[int, list[tuple[float, float, float]]],
               fps: int) -> dict[int, dict[str, Any]]:
    """Per-track spatial figures from a list of (t, x, y) in pitch metres.

    Positions are cleaned first (_clean_track). Distance then skips any step
    faster than MAX_STEP_MPS — an identity switch or a calibration jump, not
    running — and top speed is measured over one-second windows that contain no
    such step. Both are therefore conservative: they under-read a genuine burst
    rather than invent a sprint, and should be presented as approximate.
    """
    out: dict[int, dict[str, Any]] = {}

    for tid, raw in tracks.items():
        if len(raw) < 2:
            continue
        span = (max(p[0] for p in raw) - min(p[0] for p in raw))
        pts = _clean_track(raw, fps)
        if len(pts) < 2:
            continue

        dist = 0.0
        trusted_s = 0.0
        plausible = []
        for (t0, x0, y0), (t1, x1, y1) in zip(pts, pts[1:]):
            dt = t1 - t0
            step = math.hypot(x1 - x0, y1 - y0)
            ok = dt > 0 and step / dt <= MAX_STEP_MPS
            plausible.append(ok)
            if ok:
                dist += step
                trusted_s += dt

        top = None
        for i in range(len(pts) - fps):
            if not all(plausible[i:i + fps]):
                continue
            t0, x0, y0 = pts[i]
            t1, x1, y1 = pts[i + fps]
            if t1 > t0:
                v = math.hypot(x1 - x0, y1 - y0) / (t1 - t0) * 3.6
                top = v if top is None else max(top, v)
        # Judged on time whose steps were believable, not on the track's span: a
        # track can last ten seconds and have every step rejected, and its 0 m
        # would then be the filter's output, not the player's.
        measurable = trusted_s >= MIN_MOVEMENT_S

        xs = [p[1] for p in pts]
        ys = [p[2] for p in pts]
        thirds = [0, 0, 0]
        for x in xs:
            thirds[min(2, int(x / (PITCH_L / 3)))] += 1
        n = float(len(xs))

        out[tid] = {
            "distance_m": round(dist, 1) if measurable else None,
            "top_speed_kmh": round(min(top, MAX_STEP_MPS * 3.6), 1) if measurable and top is not None else None,
            "avg_position": {"x": round(sum(xs) / n, 1), "y": round(sum(ys) / n, 1)},
            "zone_share": {
                "def_third": round(thirds[0] / n, 2),
                "mid_third": round(thirds[1] / n, 2),
                "att_third": round(thirds[2] / n, 2),
            },
            "minutes_tracked": round(span / 60.0, 2),
            "_samples": pts,
        }
    return out


# Team shape from real tracking counts every outfield track followed at least
# this long. Shorter ones are re-ID fragments — a few frames of someone — and
# their average position is not a place in the team's shape.
MIN_SHAPE_TRACK_S = 5.0
# Below this many usable positions there is no shape to measure.
MIN_SHAPE_TRACKS = 4


def _team_shape(players: list[dict[str, Any]], include_unresolved: bool = False) -> dict[str, Any]:
    """Compactness, width and line height of the team's outfield positions.

    Shape does not need identity, so the real backends pass include_unresolved:
    with strict identity most tracks carry no name, and measuring only the named
    ones reduced a whole team to a single point — width 0 m, compactness 0 m —
    which the analyst would read as a measurement. The mock keeps the fixture's
    resolved-only definition (same as scripts/generate_fixtures.py) so it still
    reproduces the frozen figures exactly.

    Too few usable positions returns {} rather than zeros, for the same reason.
    """
    def usable(p: dict[str, Any]) -> bool:
        if p.get("team") == "gk" or not p.get("avg_position"):
            return False
        if include_unresolved:
            return (p.get("minutes_tracked") or 0) * 60 >= MIN_SHAPE_TRACK_S
        return bool(p.get("player_id"))

    pts = [(p["avg_position"]["x"], p["avg_position"]["y"]) for p in players if usable(p)]
    if len(pts) < MIN_SHAPE_TRACKS:
        return {}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return {"home": {
        "compactness_m": round(sum(math.dist(p, (mx, my)) for p in pts) / len(pts), 1),
        "width_m": round(max(ys) - min(ys), 1),
        "line_height_m": round(min(xs), 1),
    }}


def _heatmap_url(player_id: str) -> str | None:
    if not player_id:
        return None
    return f"{HEATMAP_BASE}/{player_id}.svg" if HEATMAP_BASE else f"sample-data/heatmaps/{player_id}.svg"


# ─────────────────────────────────────────────────────────────── backends ────
def _mock(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The frozen fixture, re-anchored onto whatever roster arrived.

    Kept deliberately: §11 says the mock stays as the demo fallback even after
    the real pipeline works.
    """
    here = Path(__file__).resolve().parent
    fixture = json.loads((here.parent / "sample-data" / "cv_result.json").read_text())
    roster = (payload.get("roster") or {}).get("players") or []

    players = []
    for i, t in enumerate(fixture["players"]):
        rp = roster[i] if (t["player_id"] and i < len(roster)) else None
        pid = (rp or {}).get("player_id") if rp else t["player_id"]
        players.append({**t,
                        "player_id": pid,
                        "jersey_number": None if t["jersey_number"] is None
                        else (rp or {}).get("jersey_number", t["jersey_number"]),
                        "heatmap_url": _heatmap_url(pid)})

    return {"job_id": job_id, "status": "done",
            "match_id": payload.get("match_id") or fixture["match_id"],
            "clip": fixture["clip"], "players": players,
            "team_shape": _team_shape(players)}


def _lite(job_id: str, payload: dict[str, Any], frames: list[Path], fps: int) -> dict[str, Any]:
    """YOLO + ByteTrack + kit-colour teams + homography.

    Fewer moving parts than sn-gamestate and no dataset conventions to satisfy,
    which makes it the backend most likely to survive a first deploy. It does
    not read jersey numbers — identity comes from the roster anchor (§7a), and
    every track is reported with the confidence that implies.
    """
    import numpy as np  # noqa: PLC0415
    import supervision as sv  # noqa: PLC0415
    from ultralytics import YOLO  # noqa: PLC0415

    model = YOLO(os.environ.get("YOLO_WEIGHTS", "yolov8m.pt"))
    tracker = sv.ByteTrack(frame_rate=fps)
    homography = _load_homography()

    tracks: dict[int, list[tuple[float, float, float]]] = {}
    crops: dict[int, list[Any]] = {}

    import cv2  # noqa: PLC0415

    for idx, frame_path in enumerate(frames):
        frame = cv2.imread(str(frame_path))
        res = model(frame, verbose=False, classes=[0])[0]  # class 0 = person
        det = sv.Detections.from_ultralytics(res)
        det = tracker.update_with_detections(det)

        for xyxy, tid in zip(det.xyxy, det.tracker_id):
            if tid is None:
                continue
            x1, y1, x2, y2 = xyxy
            # Feet, not centroid: the ground contact point is what projects
            # correctly onto the pitch plane.
            foot = np.array([[(x1 + x2) / 2.0, y2, 1.0]])
            px, py = _project(foot, homography)
            if px is None:
                continue
            tracks.setdefault(int(tid), []).append((idx / fps, px, py))
            if len(crops.get(int(tid), [])) < 12:
                crops.setdefault(int(tid), []).append(
                    frame[int(y1):int((y1 + y2) / 2), int(x1):int(x2)])

    teams = _assign_teams(crops)
    agg = _aggregate(tracks, fps)
    return _to_contract(job_id, payload, agg, teams, jersey={}, fps=fps, n_frames=len(frames))


SN_GAMESTATE_DIR = os.environ.get("SN_GAMESTATE_DIR", "/opt/sn-gamestate")
SN_GAMESTATE_PYTHON = os.environ.get("SN_GAMESTATE_PYTHON", f"{SN_GAMESTATE_DIR}/.venv/bin/python")
# Checkpoints for the detector, re-ID, jersey OCR and pitch calibration. On the
# Modal volume so they download once for the app, not once per cold start.
GAMESTATE_MODEL_DIR = os.environ.get("GAMESTATE_MODEL_DIR", "/weights/sn-gamestate")


def _gamestate(job_id: str, payload: dict[str, Any], frames: list[Path], fps: int) -> dict[str, Any]:
    """SoccerNet sn-gamestate via TrackLab.

    Tracking, re-identification, jersey-number recognition, team clustering and
    per-frame pitch localisation in one pass — the whole of §7, and the only
    backend that needs no PITCH_HOMOGRAPHY, so it tolerates a panning camera.

    Driven through TrackLab's ExternalVideo dataset rather than the SoccerNet-GSR
    layout: that takes a plain .mp4, needs no ground truth, and never triggers
    TrackLab's automatic dataset download (tens of GB). ExternalVideo reads every
    frame it is given, so it gets a clip rebuilt from the frames already sampled
    at `fps`, keeping this backend on exactly the frames `lite` would see.
    """
    work = frames[0].parent.parent
    clip = work / "clip_sampled.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
         "-i", str(frames[0].parent / "%06d.jpg"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(clip)],
        check=True,
    )

    state = work / "tracklab-state.pklz"
    log_path = work / "tracklab.log"
    Path(GAMESTATE_MODEL_DIR).mkdir(parents=True, exist_ok=True)
    cmd = [
        SN_GAMESTATE_PYTHON, "-m", "tracklab.main", "-cn", "soccernet",
        "dataset=youtube", f"dataset.video_path={clip}",
        # ExternalVideo exposes one split, "val"; soccernet.yaml asks for "valid".
        "dataset.eval_set=val",
        # No ground truth exists for a coach's clip, and no one watches the mp4.
        "eval_tracking=False", "visualization.cfg.save_videos=False",
        "use_rich=False",
        f"model_dir={GAMESTATE_MODEL_DIR}", f"data_dir={work / 'data'}",
        f"state.save_file={state}", f"experiment_name={job_id}",
        f"hydra.run.dir={work / 'tracklab-out'}",
    ]
    with open(log_path, "w") as log:
        proc = subprocess.run(cmd, cwd=SN_GAMESTATE_DIR, stdout=log, stderr=subprocess.STDOUT)
    tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
    print(tail)
    if proc.returncode != 0 or not state.exists():
        raise RuntimeError(f"sn-gamestate exited {proc.returncode}; last output:\n{tail}")

    exported = work / "tracklab-state.json"
    subprocess.run([SN_GAMESTATE_PYTHON, str(Path(__file__).with_name("gamestate_export.py")),
                    str(state), str(exported)], check=True)
    data = json.loads(exported.read_text())

    tracks, jersey, teams, home = _gamestate_tracks(data["rows"], payload, fps)
    if not tracks:
        raise RuntimeError(
            "sn-gamestate produced no localised player tracks for the coach's team "
            f"(columns seen: {data.get('columns')})")
    agg = _aggregate(tracks, fps)
    out = _to_contract(job_id, payload, agg, teams, jersey=jersey, fps=fps, n_frames=len(frames))
    out["home_team"] = home
    return out


def _home_side(payload: dict[str, Any], side_tracks: dict[str, set[int]],
               jersey: dict[int, int]) -> dict[str, Any]:
    """Which of sn-gamestate's two sides is the coach's team.

    sn-gamestate clusters kits into "left" and "right" but has no idea which one
    the coach manages, and everything downstream depends on getting it right:
    the wrong side would put the opposition's movement under this squad's names.

    In order of trust:
      1. `home_side` in the request ("left" = the team defending the left goal).
      2. Shirt numbers — but only numbers that are *distinctive*. Both teams in a
         clip usually wear 1–11, so a #9 read proves nothing; a #23 read that is
         on this roster and never appears on the other side does.

    If neither settles it the job fails with that reason, and n8n degrades to a
    report without spatial data — rather than guessing a coin flip and presenting
    it as analysis.
    """
    hint = payload.get("home_side") or (payload.get("roster") or {}).get("home_side")
    if hint in ("left", "right"):
        return {"side": hint, "basis": "home_side given in the request"}

    roster_numbers = {p.get("jersey_number") for p in (payload.get("roster") or {}).get("players") or []
                      if p.get("jersey_number") is not None}
    numbers = {side: {jersey[t] for t in tids if t in jersey} for side, tids in side_tracks.items()}
    left, right = numbers.get("left", set()), numbers.get("right", set())
    # A number read on both sides cannot tell them apart.
    left_only = (left - right) & roster_numbers
    right_only = (right - left) & roster_numbers
    if len(left_only) >= 2 and len(left_only) > 2 * len(right_only):
        return {"side": "left", "basis": f"distinctive roster shirt numbers read: {sorted(left_only)}"}
    if len(right_only) >= 2 and len(right_only) > 2 * len(left_only):
        return {"side": "right", "basis": f"distinctive roster shirt numbers read: {sorted(right_only)}"}
    raise RuntimeError(
        "could not tell which team in the clip is the coach's: shirt numbers did not "
        f"separate them (left {sorted(left)}, right {sorted(right)}). "
        'Send "home_side": "left" or "right" with the job.')


def _gamestate_tracks(rows: list[dict[str, Any]], payload: dict[str, Any], fps: int):
    """Flattened sn-gamestate detections -> (tracks, jersey, teams, home_team).

    Keeps only the coach's side, drops referees and the ball, and turns
    sn-gamestate's centre-origin pitch into §6b's corner origin with the team
    attacking left to right.
    """
    from collections import Counter  # noqa: PLC0415

    people = [r for r in rows
              if r.get("role") in ("player", "goalkeeper") and r.get("track_id") is not None
              and r.get("x") is not None and r.get("y") is not None and r.get("image_id") is not None]

    def majority(values):
        values = [v for v in values if v is not None]
        return Counter(values).most_common(1)[0][0] if values else None

    by_track: dict[int, list[dict[str, Any]]] = {}
    for r in people:
        by_track.setdefault(int(r["track_id"]), []).append(r)

    jersey: dict[int, int] = {}
    side_of: dict[int, str] = {}
    role_of: dict[int, str] = {}
    for tid, dets in by_track.items():
        num = majority([int(d["jersey_number"]) for d in dets if d.get("jersey_number") is not None])
        if num is not None:
            jersey[tid] = num
        side = majority([d.get("team") for d in dets])
        if side in ("left", "right"):
            side_of[tid] = side
        role_of[tid] = majority([d.get("role") for d in dets]) or "player"

    side_tracks: dict[str, set[int]] = {"left": set(), "right": set()}
    for tid, side in side_of.items():
        side_tracks[side].add(tid)
    home = _home_side(payload, side_tracks, jersey)

    def to_attacking_frame(x: float, y: float) -> tuple[float, float]:
        # sn-gamestate: metres from the centre spot. "left" is the team whose goal
        # is on the left of the image, so it already attacks towards +x; "right"
        # is rotated 180 degrees (not mirrored, which would swap its flanks).
        if home["side"] == "left":
            return x + PITCH_L / 2, y + PITCH_W / 2
        return PITCH_L / 2 - x, PITCH_W / 2 - y

    tracks: dict[int, list[tuple[float, float, float]]] = {}
    for tid in side_tracks[home["side"]]:
        for d in by_track[tid]:
            px, py = to_attacking_frame(float(d["x"]), float(d["y"]))
            # Calibration occasionally lands a foot point off the pitch entirely;
            # a few metres is projection noise near the lines, more is a bad frame.
            if not (-3.0 <= px <= PITCH_L + 3.0 and -3.0 <= py <= PITCH_W + 3.0):
                continue
            px = min(max(px, 0.0), PITCH_L)
            py = min(max(py, 0.0), PITCH_W)
            tracks.setdefault(tid, []).append((int(d["image_id"]) / fps, px, py))

    teams = {tid: ("gk" if role_of.get(tid) == "goalkeeper" else "home") for tid in tracks}
    jersey = {tid: n for tid, n in jersey.items() if tid in tracks}
    return tracks, jersey, teams, home


# ──────────────────────────────────────────────────────────── projection ────
def _load_homography():
    """3x3 image->pitch homography.

    The Veo mount is fixed, so this is calibrated once per camera position and
    reused. PITCH_HOMOGRAPHY is a JSON 3x3. Absent it, `lite` cannot project and
    says so rather than emitting plausible-looking nonsense.
    """
    raw = os.environ.get("PITCH_HOMOGRAPHY")
    if not raw:
        return None
    import numpy as np  # noqa: PLC0415

    return np.array(json.loads(raw), dtype="float64")


def _project(pt, h):
    if h is None:
        return None, None
    import numpy as np  # noqa: PLC0415

    v = h @ np.asarray(pt).reshape(3, 1)
    if abs(float(v[2])) < 1e-9:
        return None, None
    x, y = float(v[0] / v[2]), float(v[1] / v[2])
    if not (0 <= x <= PITCH_L and 0 <= y <= PITCH_W):
        return None, None
    return x, y


def _assign_teams(crops: dict[int, list[Any]]) -> dict[int, str]:
    """Kit-colour clustering into two teams (§7 step 4).

    Deliberately crude: two clusters over mean torso colour. It cannot tell a
    goalkeeper from an outfielder or spot the referee, so those are left to the
    roster anchor rather than guessed at.
    """
    if not crops:
        return {}
    import numpy as np  # noqa: PLC0415
    from sklearn.cluster import KMeans  # noqa: PLC0415

    ids, feats = [], []
    for tid, cs in crops.items():
        usable = [c for c in cs if getattr(c, "size", 0)]
        if not usable:
            continue
        ids.append(tid)
        feats.append(np.mean([c.reshape(-1, 3).mean(axis=0) for c in usable], axis=0))
    if len(ids) < 2:
        return {tid: "home" for tid in ids}

    labels = KMeans(n_clusters=2, n_init=10, random_state=0).fit_predict(np.array(feats))
    # The larger cluster is taken as the home side; the roster anchor corrects it.
    home = int(np.bincount(labels).argmax())
    return {tid: ("home" if lb == home else "away") for tid, lb in zip(ids, labels)}


def _to_contract(job_id, payload, agg, teams, jersey, fps, n_frames) -> dict[str, Any]:
    """Map tracks onto the roster and emit §6b.

    §7a: identity is anchored from the coach's roster, corrected by a confident
    jersey read where one exists. A track that cannot be resolved keeps
    player_id null and a low confidence — it still counts toward team shape, and
    the frontend shows that uncertainty rather than hiding it.
    """
    roster = (payload.get("roster") or {}).get("players") or []
    match_id = payload.get("match_id") or job_id

    # Shirt numbers have to be unambiguous to anchor on. A roster carrying two
    # #9s cannot resolve a #9 read, so a duplicated number matches nobody rather
    # than matching whichever player happened to come last in the list.
    by_number: dict[Any, int] = {}
    duplicated: set[Any] = set()
    for i, rp in enumerate(roster):
        num = rp.get("jersey_number")
        if num is None:
            continue
        if num in by_number:
            duplicated.add(num)
        else:
            by_number[num] = i
    for num in duplicated:
        by_number.pop(num, None)

    # Longest-lived tracks first: they are the ones worth spending a shirt on.
    ranked = sorted(agg.items(), key=lambda kv: -len(kv[1]["_samples"]))

    # Pass 1 — confident jersey reads claim their roster entry before any
    # positional guess can take it. Assigning in one greedy pass let a long
    # unidentified track consume roster[0] and push the player whose number had
    # actually been read onto a positional fallback further down.
    claimed: dict[int, int] = {}
    taken: set[int] = set()
    for tid, _ in ranked:
        idx = by_number.get(jersey.get(tid))
        if idx is not None and idx not in taken:
            taken.add(idx)
            claimed[tid] = idx

    # Pass 2 — positional fill, but only for a backend that reads no shirt
    # numbers at all (`lite`). Where OCR is available, an unread track is an
    # unknown body: naming it would print a real player's name over someone
    # else's movement, which is worse than reporting an unresolved track. This
    # is also what makes "the coach selects which players to track" meaningful —
    # an unselected player can no longer consume a selected player's slot.
    # CV_IDENTITY=roster restores §7a's original roster anchoring for every
    # backend: unread tracks take the remaining roster entries by longevity at
    # 0.55 confidence, so each player gets spatial numbers — labelled as a
    # positional guess. The default, strict, names only confident shirt reads.
    strict = bool(jersey) and os.environ.get("CV_IDENTITY", "strict") != "roster"
    spare = [i for i in range(len(roster)) if i not in taken]

    players = []
    for tid, a in ranked:
        idx = claimed.get(tid)
        if idx is not None:
            conf = 0.92                      # a confident OCR read
        elif not strict and spare:
            idx = spare.pop(0)
            conf = 0.55                      # positional assignment only
        else:
            idx, conf = None, 0.3            # a body we tracked but cannot name

        rp = roster[idx] if idx is not None else None
        pid = (rp or {}).get("player_id")
        players.append({
            "player_id": pid,
            "track_id": int(tid),
            "jersey_number": jersey.get(tid),
            "id_confidence": conf,
            "team": teams.get(tid, "home"),
            # Drawn from this track's own samples and uploaded per match. None
            # when publishing is not configured — see _publish_heatmap.
            "heatmap_url": _publish_heatmap(match_id, pid, a["_samples"], rp),
            "distance_m": a["distance_m"],
            "top_speed_kmh": a["top_speed_kmh"],
            "avg_position": a["avg_position"],
            "zone_share": a["zone_share"],
            "minutes_tracked": a["minutes_tracked"],
        })

    return {"job_id": job_id, "status": "done",
            "match_id": payload.get("match_id"),
            "clip": {"duration_s": round(n_frames / fps, 1), "fps_processed": fps},
            "players": players, "team_shape": _team_shape(players, include_unresolved=True)}


def _publish_heatmap(match_id: str, player_id: str | None, samples, rp: dict | None) -> str | None:
    """Render this track's heatmap and upload it, returning the URL.

    Deliberately returns None rather than falling back to {HEATMAP_BASE}/{id}.svg
    the way the mock does. Those files are the fixture's movement: serving them
    beside real tracking numbers would show a coach a picture of someone else's
    match and label it with their player's name.
    """
    if not player_id:
        return None
    import heatmap  # noqa: PLC0415

    return heatmap.build_and_publish(
        match_id, player_id, samples,
        name=(rp or {}).get("name") or player_id,
        number=(rp or {}).get("jersey_number"),
    )


# ─────────────────────────────────────────────────────────────── entry ──────
def run(job_id: str, payload: dict[str, Any], backend: str = "mock") -> dict[str, Any]:
    if backend == "mock":
        return _mock(job_id, payload)

    fps = int(payload.get("fps") or 5)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        clip = _download(payload["clip_url"], work / "clip.mp4")
        frames = _extract_frames(clip, work / "frames", fps, float(payload.get("duration_s") or 90))
        if not frames:
            raise RuntimeError("ffmpeg produced no frames — is clip_url a video?")

        if backend == "lite":
            return _lite(job_id, payload, frames, fps)
        if backend == "gamestate":
            return _gamestate(job_id, payload, frames, fps)
    raise ValueError(f"unknown CV_BACKEND {backend!r}")
