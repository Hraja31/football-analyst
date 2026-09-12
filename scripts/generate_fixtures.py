#!/usr/bin/env python3
"""Generate sample-data/cv_result.json + per-player SVG heatmaps from roster.json.

Pure stdlib on purpose: no numpy/matplotlib, so this runs anywhere, instantly.
Positions follow the BUILD_SPEC 6b contract on a 105x68 m pitch (x = 0-105 along
the length, attacking left-to-right; y = 0-68 across).

Regenerate with a real asset host once Supabase Storage exists:
    python3 scripts/generate_fixtures.py --base-url https://<proj>.supabase.co/storage/v1/object/public/heatmaps
"""
import argparse, json, math, os, random

PITCH_X, PITCH_Y = 105.0, 68.0
CELL = 3.0                      # heatmap cell size in metres
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


def density_grid(pts):
    nx, ny = int(PITCH_X / CELL), int(PITCH_Y / CELL)
    g = [[0.0] * nx for _ in range(ny)]
    for x, y in pts:
        g[min(int(y / CELL), ny - 1)][min(int(x / CELL), nx - 1)] += 1.0
    # one box-blur pass so the plot reads as heat, not confetti
    out = [[0.0] * nx for _ in range(ny)]
    for j in range(ny):
        for i in range(nx):
            tot = cnt = 0.0
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    jj, ii = j + dj, i + di
                    if 0 <= jj < ny and 0 <= ii < nx:
                        tot += g[jj][ii]; cnt += 1
            out[j][i] = tot / cnt
    return out


def heat_colour(t):
    """t in 0..1 -> transparent blue -> green -> yellow -> red."""
    stops = [(0.0, (24, 60, 130)), (0.35, (28, 150, 120)),
             (0.65, (235, 200, 60)), (1.0, (214, 48, 42))]
    for k in range(len(stops) - 1):
        t0, c0 = stops[k]; t1, c1 = stops[k + 1]
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0)
            return tuple(round(c0[m] + (c1[m] - c0[m]) * f) for m in range(3))
    return stops[-1][1]


def render_svg(grid, name, number, avg):
    S = 8  # px per metre
    W, H = PITCH_X * S, PITCH_Y * S
    peak = max(max(r) for r in grid) or 1.0
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" width="{W:.0f}" height="{H:.0f}">',
         f'<rect width="{W:.0f}" height="{H:.0f}" fill="#0f2417"/>']
    for j, row in enumerate(grid):
        for i, v in enumerate(row):
            if v <= 0.01:
                continue
            t = (v / peak) ** 0.65
            if t < 0.06:
                continue
            r, g, b = heat_colour(t)
            p.append(f'<rect x="{i*CELL*S:.1f}" y="{j*CELL*S:.1f}" width="{CELL*S:.1f}" '
                     f'height="{CELL*S:.1f}" fill="rgb({r},{g},{b})" opacity="{0.18+0.72*t:.2f}"/>')
    L = 'fill="none" stroke="rgba(255,255,255,.55)" stroke-width="2"'
    p += [f'<rect x="{S}" y="{S}" width="{W-2*S:.0f}" height="{H-2*S:.0f}" {L}/>',
          f'<line x1="{W/2:.0f}" y1="{S}" x2="{W/2:.0f}" y2="{H-S:.0f}" {L}/>',
          f'<circle cx="{W/2:.0f}" cy="{H/2:.0f}" r="{9.15*S:.0f}" {L}/>',
          f'<rect x="{S}" y="{(34-20.15)*S:.0f}" width="{16.5*S:.0f}" height="{40.3*S:.0f}" {L}/>',
          f'<rect x="{W-S-16.5*S:.0f}" y="{(34-20.15)*S:.0f}" width="{16.5*S:.0f}" height="{40.3*S:.0f}" {L}/>',
          f'<circle cx="{avg[0]*S:.0f}" cy="{avg[1]*S:.0f}" r="7" fill="#fff" stroke="#111" stroke-width="2"/>',
          f'<text x="{S+10}" y="{H-18}" fill="rgba(255,255,255,.85)" font-family="system-ui,sans-serif" '
          f'font-size="22" font-weight="600">#{number} {name}</text>',
          '</svg>']
    return "\n".join(p)


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
