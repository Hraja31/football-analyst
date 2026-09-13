"""Position heatmaps: density grid -> SVG -> Supabase Storage.

One renderer, shared by the real pipeline and by scripts/generate_fixtures.py,
so a mock report and a real report draw the same picture from the same numbers.
Before this existed only the fixture had heatmaps, and `lite`/`gamestate` linked
to them regardless of what they had actually tracked.

Pure stdlib on purpose: this runs inside the GPU container and inside a throwaway
fixture script, and neither should need matplotlib to draw a grid of rectangles.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

PITCH_L, PITCH_W = 105.0, 68.0
CELL = 3.0  # heatmap cell size in metres


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def density_grid(points: Iterable[tuple[float, float]]) -> list[list[float]]:
    """Occupancy per 3 m cell, box-blurred once so it reads as heat, not confetti."""
    nx, ny = int(PITCH_L / CELL), int(PITCH_W / CELL)
    g = [[0.0] * nx for _ in range(ny)]
    for x, y in points:
        x = _clamp(x, 0.0, PITCH_L - 0.01)
        y = _clamp(y, 0.0, PITCH_W - 0.01)
        g[min(int(y / CELL), ny - 1)][min(int(x / CELL), nx - 1)] += 1.0

    out = [[0.0] * nx for _ in range(ny)]
    for j in range(ny):
        for i in range(nx):
            tot = cnt = 0.0
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    jj, ii = j + dj, i + di
                    if 0 <= jj < ny and 0 <= ii < nx:
                        tot += g[jj][ii]
                        cnt += 1
            out[j][i] = tot / cnt
    return out


def heat_colour(t: float) -> tuple[int, int, int]:
    """t in 0..1 -> blue -> green -> yellow -> red."""
    stops = [(0.0, (24, 60, 130)), (0.35, (28, 150, 120)),
             (0.65, (235, 200, 60)), (1.0, (214, 48, 42))]
    for k in range(len(stops) - 1):
        t0, c0 = stops[k]
        t1, c1 = stops[k + 1]
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0)
            return tuple(round(c0[m] + (c1[m] - c0[m]) * f) for m in range(3))
    return stops[-1][1]


def render_svg(grid: list[list[float]], name: str, number: Any, avg: tuple[float, float]) -> str:
    S = 8  # px per metre
    W, H = PITCH_L * S, PITCH_W * S
    peak = max((max(r) for r in grid), default=0.0) or 1.0
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


# ────────────────────────────────────────────────────────────── publishing ────
BUCKET = os.environ.get("HEATMAP_BUCKET", "heatmaps")


def configured() -> bool:
    """True when this process can actually upload a heatmap."""
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def publish(match_id: str, player_id: str, svg: str) -> str | None:
    """Upload one SVG and return its public URL, or None if that did not happen.

    Namespaced by match: every match draws its own p_07.svg, and without the
    prefix each new match would overwrite the last one's images and silently
    rewrite the heatmaps on every report already issued.

    Returns None rather than raising. A missing heatmap costs one image; a raised
    exception costs the whole job, and the spatial numbers are the valuable part.
    """
    if not configured():
        return None

    import requests  # noqa: PLC0415

    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    path = f"{match_id}/{player_id}.svg"
    try:
        r = requests.post(
            f"{base}/storage/v1/object/{BUCKET}/{path}",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "image/svg+xml", "x-upsert": "true"},
            data=svg.encode("utf-8"), timeout=30,
        )
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"heatmap upload failed for {path}: {type(exc).__name__}: {exc}")
        return None
    return f"{base}/storage/v1/object/public/{BUCKET}/{path}"


def build_and_publish(match_id: str, player_id: str, samples, name: str, number: Any) -> str | None:
    """Draw one player's heatmap from their track samples and upload it."""
    pts = [(x, y) for _, x, y in samples]
    if not pts:
        return None
    avg = (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))
    svg = render_svg(density_grid(pts), name or player_id, number if number is not None else "", avg)
    return publish(match_id, player_id, svg)
