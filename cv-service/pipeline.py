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
def _download(url: str, dest: Path) -> Path:
    import requests  # noqa: PLC0415

    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
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


def _aggregate(tracks: dict[int, list[tuple[float, float, float]]],
               fps: int) -> dict[int, dict[str, Any]]:
    """Per-track spatial figures from a list of (t, x, y) in pitch metres.

    Speed is computed over a half-second window rather than frame to frame:
    at 5 fps a one-pixel jitter in the projection turns into an implausible
    sprint, and a top speed of 60 km/h in the report would discredit everything
    next to it.
    """
    out: dict[int, dict[str, Any]] = {}
    win = max(1, fps // 2)

    for tid, pts in tracks.items():
        if len(pts) < 2:
            continue
        pts = sorted(pts)
        dist = 0.0
        for (_, x0, y0), (_, x1, y1) in zip(pts, pts[1:]):
            dist += math.hypot(x1 - x0, y1 - y0)

        top = 0.0
        for i in range(len(pts) - win):
            t0, x0, y0 = pts[i]
            t1, x1, y1 = pts[i + win]
            dt = t1 - t0
            if dt > 0:
                top = max(top, math.hypot(x1 - x0, y1 - y0) / dt * 3.6)

        xs = [p[1] for p in pts]
        ys = [p[2] for p in pts]
        thirds = [0, 0, 0]
        for x in xs:
            thirds[min(2, int(x / (PITCH_L / 3)))] += 1
        n = float(len(xs))

        out[tid] = {
            "distance_m": round(dist, 1),
            "top_speed_kmh": round(min(top, 40.0), 1),
            "avg_position": {"x": round(sum(xs) / n, 1), "y": round(sum(ys) / n, 1)},
            "zone_share": {
                "def_third": round(thirds[0] / n, 2),
                "mid_third": round(thirds[1] / n, 2),
                "att_third": round(thirds[2] / n, 2),
            },
            "minutes_tracked": round((pts[-1][0] - pts[0][0]) / 60.0, 2),
            "_samples": pts,
        }
    return out


def _team_shape(players: list[dict[str, Any]]) -> dict[str, Any]:
    """Same definitions as scripts/generate_fixtures.py, so the real pipeline and
    the fixture describe shape the same way and the numbers stay comparable."""
    pts = [(p["avg_position"]["x"], p["avg_position"]["y"])
           for p in players if p.get("player_id") and p.get("team") != "gk"]
    if not pts:
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


def _gamestate(job_id: str, payload: dict[str, Any], frames: list[Path], fps: int) -> dict[str, Any]:
    """SoccerNet sn-gamestate via TrackLab.

    This gives tracking, re-identification, jersey-number recognition and pitch
    localisation in a single pass — the whole of what §7 asks for.

    The awkward part, and the reason `lite` exists alongside it: sn-gamestate is
    built around the SoccerNet-GSR dataset layout, not loose mp4s. So the frames
    are written into that layout first and TrackLab is pointed at it.
    """
    work = frames[0].parent.parent
    seq = work / "SNGS-live" / "img1"
    seq.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(frames, start=1):
        os.link(f, seq / f"{i:06d}.jpg")

    (seq.parent / "Labels-GameState.json").write_text(json.dumps({
        "info": {"version": "0.1", "im_dir": "img1", "frame_rate": fps,
                 "seq_length": len(frames), "im_ext": ".jpg", "name": "SNGS-live"},
        "images": [{"file_name": f"{i:06d}.jpg", "image_id": str(i), "frame_id": i}
                   for i in range(1, len(frames) + 1)],
        "annotations": [], "categories": [],
    }))

    out_dir = work / "tracklab-out"
    subprocess.run(
        ["python", "-m", "tracklab.main", "-cn", "soccernet",
         f"dataset.dataset_path={work}", f"dataset.eval_set=live",
         "visualization.save_videos=False", f"experiment_name={job_id}",
         f"hydra.run.dir={out_dir}"],
        check=True, cwd=os.environ.get("SN_GAMESTATE_DIR", "/opt/sn-gamestate"),
    )

    tracks, jersey = _read_tracklab(out_dir, fps)
    agg = _aggregate(tracks, fps)
    teams = {}  # sn-gamestate assigns roles itself; carried through by _read_tracklab
    return _to_contract(job_id, payload, agg, teams, jersey=jersey, fps=fps, n_frames=len(frames))


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


def _read_tracklab(out_dir: Path, fps: int):
    """Read TrackLab's per-frame output into (t, x, y) samples plus jersey reads."""
    tracks: dict[int, list[tuple[float, float, float]]] = {}
    jersey: dict[int, int] = {}
    candidates = list(out_dir.rglob("*.json"))
    if not candidates:
        raise RuntimeError(f"sn-gamestate produced no output under {out_dir}")

    data = json.loads(max(candidates, key=lambda p: p.stat().st_size).read_text())
    for det in data.get("predictions", data.get("annotations", [])):
        tid = det.get("track_id")
        pitch = det.get("bbox_pitch") or {}
        x, y = pitch.get("x_bottom_middle"), pitch.get("y_bottom_middle")
        if tid is None or x is None or y is None:
            continue
        # TrackLab centres the pitch on (0,0); §6b uses a corner origin.
        tracks.setdefault(int(tid), []).append(
            (int(det.get("image_id", 0)) / fps, float(x) + PITCH_L / 2, float(y) + PITCH_W / 2))
        num = det.get("jersey_number")
        if num not in (None, ""):
            try:
                jersey[int(tid)] = int(num)
            except (TypeError, ValueError):
                pass
    return tracks, jersey


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
    strict = bool(jersey)
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
            "players": players, "team_shape": _team_shape(players)}


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
