#!/usr/bin/env python3
"""Generate sample-data/cv_result.json + per-player SVG heatmaps from roster.json.

Pure stdlib on purpose: no numpy/matplotlib, so this runs anywhere, instantly.
Positions follow the BUILD_SPEC 6b contract on a 105x68 m pitch (x = 0-105 along
the length, attacking left-to-right; y = 0-68 across).

Regenerate with a real asset host once Supabase Storage exists:
    python3 scripts/generate_fixtures.py --base-url https://<proj>.supabase.co/storage/v1/object/public/heatmaps
"""
import argparse, json, math, os, random, sys

# One renderer, shared with the CV service, so the fixture and a real match draw
# the same picture from the same numbers. cv-service is a sibling directory
# rather than an installed package, hence the path insert.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cv-service"))
from heatmap import CELL, density_grid, heat_colour, render_svg  # noqa: E402

PITCH_X, PITCH_Y = 105.0, 68.0
SAMPLES = 900                   # touch/position samples per player
SEED = 20260913

# Per-position movement model: (centre_x, centre_y, spread_x, spread_y, intensity)
# intensity ~ how much ground they cover, scales distance_m and top speed.
ROLE = {
    "GK":  (6.0, 34.0,  4.0,  7.0, 0.35),
    "RB":  (58.0, 58.0, 18.0,  8.0, 1.05),
    "LB":  (58.0, 10.0, 18.0,  8.0, 1.05),
    "CB":  (34.0, 34.0, 12.0,  9.0, 0.80),
    "CDM": (48.0, 34.0, 14.0, 11.0, 0.95),
    "CM":  (62.0, 34.0, 16.0, 12.5, 1.15),
    "ST":  (84.0, 34.0, 12.0, 12.0, 0.95),
    "LW":  (76.0, 12.0, 16.0,  9.0, 1.10),
    "RW":  (76.0, 56.0, 16.0,  9.0, 1.10),
}
# Nudge the two CBs apart so the pair reads as a partnership, not one blob.
CB_SPLIT = {"p_03": +8.0, "p_04": -8.0}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sample_points(rng, cx, cy, sx, sy, n):
    pts = []
    for _ in range(n):
        x = clamp(rng.gauss(cx, sx), 1.0, PITCH_X - 1.0)
        y = clamp(rng.gauss(cy, sy), 1.0, PITCH_Y - 1.0)
        pts.append((x, y))
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="sample-data/heatmaps",
                    help="URL prefix written into heatmap_url (Supabase Storage URL in production)")
    ap.add_argument("--duration", type=float, default=60.0, help="clip length in seconds")
    a = ap.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roster = json.load(open(os.path.join(here, "sample-data", "roster.json")))
    out_dir = os.path.join(here, "sample-data", "heatmaps")
    os.makedirs(out_dir, exist_ok=True)

    rng = random.Random(SEED)
    minutes = a.duration / 60.0
    players = []

    for idx, pl in enumerate(roster["players"]):
        cx, cy, sx, sy, intensity = ROLE[pl["position"]]
        cy += CB_SPLIT.get(pl["player_id"], 0.0)
        pts = sample_points(rng, cx, cy, sx, sy, SAMPLES)
        grid = density_grid(pts)

        ax = sum(x for x, _ in pts) / len(pts)
        ay = sum(y for _, y in pts) / len(pts)
        thirds = [0, 0, 0]
        for x, _ in pts:
            thirds[0 if x < 35 else (1 if x < 70 else 2)] += 1
        n = float(len(pts))

        # 60s of active play: ~165 m/min at a jog, scaled by role intensity.
        distance = round(165.0 * minutes * intensity * rng.uniform(0.88, 1.12), 1)
        top_speed = round((14.0 if pl["position"] == "GK" else 24.0) * rng.uniform(0.92, 1.16), 1)
        conf = round(rng.uniform(0.71, 0.94), 2)

        open(os.path.join(out_dir, f'{pl["player_id"]}.svg'), "w").write(
            render_svg(grid, pl["name"], pl["jersey_number"], (ax, ay)))

        players.append({
            "player_id": pl["player_id"],
            "track_id": idx + 1,
            "jersey_number": pl["jersey_number"] if conf > 0.80 else None,
            "id_confidence": conf,
            "team": "gk" if pl["position"] == "GK" else "home",
            "heatmap_url": f'{a.base_url.rstrip("/")}/{pl["player_id"]}.svg',
            "distance_m": distance,
            "top_speed_kmh": top_speed,
            "avg_position": {"x": round(ax, 1), "y": round(ay, 1)},
            "zone_share": {"def_third": round(thirds[0]/n, 2),
                           "mid_third": round(thirds[1]/n, 2),
                           "att_third": round(thirds[2]/n, 2)},
            "minutes_tracked": round(minutes, 2),
        })

    # One deliberately unresolved track: spec 7a says these exist and must be
    # honest (player_id null, low confidence) so Claude treats them as shape-only.
    players.append({
        "player_id": None, "track_id": 12, "jersey_number": None, "id_confidence": 0.31,
        "team": "home", "heatmap_url": None, "distance_m": 141.8, "top_speed_kmh": 22.4,
        "avg_position": {"x": 55.2, "y": 31.6},
        "zone_share": {"def_third": 0.31, "mid_third": 0.52, "att_third": 0.17},
        "minutes_tracked": round(minutes, 2),
    })

    xs = [p["avg_position"]["x"] for p in players if p["player_id"] and p["team"] != "gk"]
    ys = [p["avg_position"]["y"] for p in players if p["player_id"] and p["team"] != "gk"]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)

    cv = {
        "job_id": "job_abc123",
        "status": "done",
        "match_id": roster["match_id"],
        "clip": {"duration_s": a.duration, "fps_processed": 5},
        "players": players,
        "team_shape": {"home": {
            "compactness_m": round(sum(math.dist((x, y), (mx, my)) for x, y in zip(xs, ys))/len(xs), 1),
            "width_m": round(max(ys) - min(ys), 1),
            "line_height_m": round(min(xs), 1),
        }},
    }
    path = os.path.join(here, "sample-data", "cv_result.json")
    json.dump(cv, open(path, "w"), indent=2)
    print(f"wrote {path}")
    print(f"wrote {len(roster['players'])} heatmaps to {out_dir}")
    print(f"team shape: {cv['team_shape']['home']}")


if __name__ == "__main__":
    main()
