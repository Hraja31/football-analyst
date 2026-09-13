"""TrackLab tracker state (.pklz) -> plain JSON.

Runs under sn-gamestate's own interpreter (/opt/sn-gamestate/.venv/bin/python,
Python 3.9), not the service's. The state is a zip of pickled pandas DataFrames,
and a pickle is only reliably readable by the pandas that wrote it — the service
image runs a different Python and a different pandas. So this is kept to the
3.9 subset of the language and does nothing but flatten rows.

    python gamestate_export.py <state.pklz> <out.json>
"""
import json
import math
import sys
import zipfile

import pandas as pd


def _clean(value):
    """numpy scalars and NaN -> JSON-safe Python values."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def main(state_path, out_path):
    rows = []
    columns = []
    with zipfile.ZipFile(state_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".pkl") and not n.endswith("_image.pkl")]
        for name in names:
            with zf.open(name) as fp:
                df = pd.read_pickle(fp)
            columns = sorted(set(columns) | set(df.columns))
            for rec in df.to_dict("records"):
                pitch = rec.get("bbox_pitch")
                x = y = None
                if isinstance(pitch, dict):
                    x = _clean(pitch.get("x_bottom_middle"))
                    y = _clean(pitch.get("y_bottom_middle"))
                rows.append({
                    "image_id": _clean(rec.get("image_id")),
                    "track_id": _clean(rec.get("track_id")),
                    "x": x,
                    "y": y,
                    "jersey_number": _clean(rec.get("jersey_number")),
                    "role": _clean(rec.get("role")),
                    "team": _clean(rec.get("team")),
                })

    with open(out_path, "w") as fh:
        # columns are kept so a schema change upstream shows up in the job error,
        # not as a silently empty result.
        json.dump({"columns": columns, "rows": rows}, fh)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
