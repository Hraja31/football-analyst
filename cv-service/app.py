"""FastAPI surface for the CV service — BUILD_SPEC §7.

Two endpoints, and nothing else n8n depends on:

    POST /process        {clip_url, roster}  -> {job_id}
    GET  /result/{id}                        -> the §6b contract

The §6b shape is frozen. Whatever happens inside this service, the JSON that
leaves it must not change, because workflow node 4 parses it directly and a
change here is a silent break there.
"""
from __future__ import annotations

import hmac
import os
import time
import traceback
import uuid
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

api = FastAPI(title="Footage to Tactics — CV service", version="1.0.0")

# Backends, cheapest first. See pipeline.py.
#   mock       the frozen fixture; no GPU, no clip. The demo fallback.
#   lite       YOLO + ByteTrack + kit clustering + homography. Runs on one GPU.
#   gamestate  SoccerNet sn-gamestate via TrackLab. Highest fidelity, heaviest.
BACKEND = os.environ.get("CV_BACKEND", "mock")

# Shared secret between n8n and this service. On `gamestate` every accepted job
# rents a GPU, so an open /process is an open bill. Unset means unauthenticated —
# kept possible so the service can deploy before n8n holds the token, and
# reported by /health so an open service is visible rather than assumed closed.
CV_API_TOKEN = os.environ.get("CV_API_TOKEN", "")


def require_token(x_cv_token: str | None = Header(default=None)) -> None:
    if not CV_API_TOKEN:
        return
    if not x_cv_token or not hmac.compare_digest(x_cv_token, CV_API_TOKEN):
        raise HTTPException(401, "missing or invalid X-CV-Token")


class ProcessRequest(BaseModel):
    clip_url: str | None = None
    match_id: str | None = None
    roster: dict[str, Any] = Field(default_factory=dict)
    # 30-90 s per §2; longer clips are trimmed rather than refused, so a coach
    # uploading a full half gets the first 90 s instead of an error.
    duration_s: float = 90.0
    fps: int = 5
    # Which side of the camera view the coach's team defends: "left" or "right".
    # sn-gamestate separates the two teams but cannot know which one is ours;
    # without this, `gamestate` has to find distinctive shirt numbers or fail.
    home_side: Literal["left", "right"] | None = None


JOBS_DICT = "f2t-cv-jobs"
MODAL_APP = "footage-to-tactics-cv"


def _store():
    """Job store: a Modal Dict in the cloud, a plain dict when run locally.

    Looked up by name rather than by importing modal_app, which would re-execute
    the whole app definition inside the container.
    """
    try:
        import modal  # noqa: PLC0415

        return modal.Dict.from_name(JOBS_DICT, create_if_missing=True)
    except Exception:
        global _LOCAL_JOBS
        try:
            return _LOCAL_JOBS
        except NameError:
            _LOCAL_JOBS = {}
            return _LOCAL_JOBS


@api.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "backend": BACKEND, "auth": bool(CV_API_TOKEN)}


@api.post("/process", dependencies=[Depends(require_token)])
def process(req: ProcessRequest) -> dict[str, Any]:
    """Accept a job and return immediately. Node 4 polls for the result."""
    if BACKEND != "mock" and not req.clip_url:
        raise HTTPException(400, "clip_url is required unless CV_BACKEND=mock")
    # Refused here rather than inside the GPU job: file://, internal hosts and
    # other schemes never reach a container, and the mistake costs no GPU time.
    if BACKEND != "mock" and not req.clip_url.lower().startswith(("https://", "http://")):
        raise HTTPException(400, "clip_url must be an http(s) link to the video file")

    job_id = "job_" + uuid.uuid4().hex[:8]
    store = _store()
    store[job_id] = {
        "status": "processing",
        "match_id": req.match_id or req.roster.get("match_id"),
        "created_at": time.time(),
    }

    payload = req.model_dump()
    if BACKEND == "mock":
        # The mock is a JSON file re-labelled onto the roster. Spawning run_job for
        # it would start an A10G container to read a fixture.
        _run_inline(job_id, payload)
        return {"job_id": job_id, "status": "processing", "match_id": payload["match_id"]}

    try:
        import modal  # noqa: PLC0415

        # Detached: the HTTP response must not wait on a GPU job that may take
        # minutes. This is exactly why node 4 is a polling loop.
        modal.Function.from_name(MODAL_APP, "run_job").spawn(job_id, payload)
    except Exception:
        # Local/dev with no GPU function deployed: run inline.
        _run_inline(job_id, payload)

    return {"job_id": job_id, "status": "processing", "match_id": payload["match_id"]}


def _run_inline(job_id: str, payload: dict[str, Any]) -> None:
    import pipeline  # noqa: PLC0415

    store = _store()
    try:
        store[job_id] = pipeline.run(job_id, payload, backend=BACKEND)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        store[job_id] = {
            "job_id": job_id,
            "status": "failed",
            "match_id": payload.get("match_id"),
            "error": f"{type(exc).__name__}: {exc}",
        }


@api.get("/result", dependencies=[Depends(require_token)])
def result_by_query(job_id: str) -> dict[str, Any]:
    """`GET /result?job_id=` — the form n8n's node 4 calls.

    Both spellings are served because the n8n mock CV workflow only answers the
    query form, and the whole point of the §6b contract is that swapping CV
    backends is a change to CV_SERVICE_URL in node 2 and nothing else. With only
    the path form here, pointing the pipeline back at the mock would 404 every
    poll and ride out the full polling window before degrading.
    """
    return result(job_id)


@api.get("/result/{job_id}", dependencies=[Depends(require_token)])
def result(job_id: str) -> dict[str, Any]:
    """Return the §6b contract.

    A job we have never seen is reported as failed rather than invented. Node 4b
    treats that the same as a dead service and the pipeline degrades cleanly, so
    an honest failure costs the coach spatial data but never the report.
    """
    rec = _store().get(job_id)
    if rec is None:
        return {"job_id": job_id, "status": "failed", "error": "unknown job_id"}
    out = dict(rec)
    out.setdefault("job_id", job_id)
    return out
