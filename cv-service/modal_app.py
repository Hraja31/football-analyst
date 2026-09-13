"""Modal deployment for the CV service.

    modal deploy modal_app.py

Gives you two things:
  * a web endpoint serving app.py  ->  point node 2's CV_SERVICE_URL at it
  * a GPU function that runs the clip out of band

The split matters. /process must answer in milliseconds because node 13 has
already told the coach's browser the job was accepted; the GPU work happens in
a spawned function and node 4 polls for it.
"""
from __future__ import annotations

import os

import modal

# Pinned upstreams for the "gamestate" backend. Bump deliberately, not by drift.
SN_GAMESTATE_SHA = "1c958345067218297d221e45e1a6405f975f83e0"

app = modal.App("footage-to-tactics-cv")

# Job state shared between the web endpoint and the GPU function. A Modal Dict
# rather than process memory, because they are different containers.
jobs = modal.Dict.from_name("f2t-cv-jobs", create_if_missing=True)

# Model weights live on a volume so they are downloaded once, not per cold start.
weights = modal.Volume.from_name("f2t-cv-weights", create_if_missing=True)

BASE = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git", "libgl1", "libglib2.0-0")
    .pip_install(
        "fastapi[standard]==0.115.6",
        "pydantic==2.10.4",
        "requests==2.32.3",
        "numpy==1.26.4",
    )
)

# The CV stack is only needed by the GPU function, so the web container stays
# small and cold-starts fast.
GPU_IMAGE = (
    BASE.pip_install(
        "torch==2.4.1",
        "torchvision==0.19.1",
        "opencv-python-headless==4.10.0.84",
        "ultralytics==8.3.40",
        "supervision==0.25.1",
        "scikit-learn==1.5.2",
    )
    # sn-gamestate, installed the way upstream supports and nothing else works:
    #
    #   * with uv, not pip — its calibration plugin is a local path declared in
    #     [tool.uv.sources], which pip cannot see ("No matching distribution
    #     found for tracklab_calibration");
    #   * on Python 3.9 — it pins requires-python ">=3.9,<3.10" and Modal's
    #     builder starts at 3.10, so uv provisions the interpreter itself;
    #   * from uv.lock (--frozen), so torch 1.13.1 and the OpenMMLab stack come
    #     out exactly as upstream resolved them rather than as pip backtracks;
    #   * with mmcv as a prebuilt wheel for torch 1.13 / cu117, which is what
    #     upstream's `mim install mmcv==2.0.1` fetches. --no-build makes a
    #     missing wheel fail loudly instead of starting a long source build.
    #
    # It lives in its own venv, so it never touches the `lite` stack above;
    # pipeline._gamestate calls that venv's python.
    .env({
        "UV_PYTHON_INSTALL_DIR": "/opt/uv-python",
        "UV_LINK_MODE": "copy",
        "UV_NO_CACHE": "1",
        "SN_GAMESTATE_DIR": "/opt/sn-gamestate",
        # Checkpoints fetched on first run land on the volume, once for the app.
        "HF_HOME": "/weights/hf",
        "TORCH_HOME": "/weights/torch",
    })
    .run_commands(
        "pip install 'uv>=0.5'",
        "git clone https://github.com/SoccerNet/sn-gamestate.git /opt/sn-gamestate",
        f"cd /opt/sn-gamestate && git checkout {SN_GAMESTATE_SHA}",
        "cd /opt/sn-gamestate && uv venv --python 3.9 .venv",
        "cd /opt/sn-gamestate && uv sync --frozen --no-dev",
        "uv pip install --python /opt/sn-gamestate/.venv/bin/python mmcv==2.0.1 "
        "--find-links https://download.openmmlab.com/mmcv/dist/cu117/torch1.13/index.html "
        "--no-build",
        # Fail the build, not the first coach's job, if the stack does not import.
        "/opt/sn-gamestate/.venv/bin/python -c \"import torch, mmcv, mmdet, mmocr, tracklab, "
        "sn_gamestate; print('sn-gamestate stack:', torch.__version__, mmcv.__version__)\"",
    )
    # Upstream fix, applied in the image. The calibration modules read the batch's
    # first row as `metadatas["keypoints"][0]`. On SoccerNet's own dataset the
    # image ids are strings, so pandas treats [0] as "first row". TrackLab's
    # ExternalVideo — the loader for an arbitrary clip — uses integer frame ids,
    # where [0] means "the row labelled 0", which exists only in the first batch:
    # every later frame raised KeyError: 0. The same functions already use
    # .iloc[0] a few lines further down; this makes the lookups agree. Its own
    # layer, so it does not rebuild the install above. The grep fails the build
    # if an instance survives.
    .run_commands(
        "sed -i -E 's/metadatas\\[\"([a-z_]+)\"\\]\\[0\\]/metadatas[\"\\1\"].iloc[0]/g' "
        "/opt/sn-gamestate/sn_gamestate/calibration/*.py",
        "! grep -rnE 'metadatas\\[\"[a-z_]+\"\\]\\[0\\]' /opt/sn-gamestate/sn_gamestate",
    )
)

# app.py / pipeline.py are shipped into the image itself. add_local_dir is the
# current API; modal.Mount is deprecated.
_HERE = os.path.dirname(os.path.abspath(__file__))
# pipeline._mock reads ../sample-data/cv_result.json relative to pipeline.py, so
# the fixture has to land at /root/sample-data — shipping /root/cv alone leaves
# the mock backend raising FileNotFoundError inside the container.
_SAMPLE = os.path.join(os.path.dirname(_HERE), "sample-data")
BASE = (BASE.add_local_dir(_HERE, remote_path="/root/cv")
            .add_local_dir(_SAMPLE, remote_path="/root/sample-data"))
GPU_IMAGE = (GPU_IMAGE.add_local_dir(_HERE, remote_path="/root/cv")
                      .add_local_dir(_SAMPLE, remote_path="/root/sample-data"))

# CV_BACKEND and HEATMAP_BASE arrive from this secret. Without it attached the
# containers would silently fall back to defaults — create it before deploying:
#   modal secret create f2t-cv CV_BACKEND=mock HEATMAP_BASE=...
CONFIG = modal.Secret.from_name("f2t-cv")

_common = dict(volumes={"/weights": weights}, secrets=[CONFIG])


@app.function(
    image=GPU_IMAGE,
    gpu=os.environ.get("MODAL_GPU", "A10G"),
    timeout=1800,
    **_common,
)
def run_job(job_id: str, payload: dict) -> None:
    """Run one clip. Failures are recorded, never raised at the caller.

    Node 4b already handles a CV service that dies, but a job that fails
    silently would leave node 4 polling until it times out — six wasted polls
    and a slower demo. Recording the failure lets the pipeline degrade at once.
    """
    import sys

    sys.path.insert(0, "/root/cv")
    import pipeline  # noqa: PLC0415

    backend = os.environ.get("CV_BACKEND", "lite")
    try:
        jobs[job_id] = pipeline.run(job_id, payload, backend=backend)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        jobs[job_id] = {
            "job_id": job_id,
            "status": "failed",
            "match_id": payload.get("match_id"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        # Persist any checkpoints downloaded during the job, so the next cold
        # start finds them instead of fetching them again.
        try:
            weights.commit()
        except Exception:  # noqa: BLE001
            pass


@app.function(image=BASE, **_common)
@modal.asgi_app()
def web():
    import sys

    sys.path.insert(0, "/root/cv")
    from app import api  # noqa: PLC0415

    return api
