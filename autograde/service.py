"""Local HTTP service for the Premiere panel.

Security model
  * Binds to loopback only (refuses anything else).
  * Every endpoint except ``/health`` requires the per-install token, sent as the
    ``X-CG-Token`` header (or ``?token=`` for <img>/report links that can't set
    headers). The token is created on first start in ``<workdir>/service-token``
    (mode 600) and printed once; paste it into the panel.
  * No CORS by default (browsers can't read responses); add origins explicitly
    with ``--allow-origin`` if a host requires it.
  * Manifest media paths must be absolute, existing video files; the reference
    must be an existing image or a shot id. Source media is only ever read.
  * An Anthropic API key sent by the panel is used for that job only and never
    written to disk or echoed in logs/errors.

Endpoints
  GET  /health                         version, ffmpeg availability (no auth)
  POST /ai/check-key                   {"api_key"} -> validate without spending tokens
  POST /analyze                        {"manifest", "options", "api_key"?} -> {"job_id"}
  GET  /jobs/{id}                      status, progress, log, summary
  POST /jobs/{id}/cancel               cooperative cancel
  POST /jobs/{id}/approvals            {"shot_001": true, ...} -> saved to approvals.json
  GET  /results/{id}/...               files written by the job
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import threading
import traceback
import uuid
from dataclasses import fields
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, ai
from .media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .pipeline import Cancelled, Options, run, shots_from_manifest

TOKEN_FILE = "service-token"


class AnalyzeRequest(BaseModel):
    manifest: dict
    options: dict = {}
    # Optional Anthropic API key from the panel. Held in memory for the job only.
    api_key: str | None = None


class KeyCheck(BaseModel):
    api_key: str | None = None


def load_or_create_token(workdir: Path) -> str:
    path = workdir / TOKEN_FILE
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def validate_request(req: AnalyzeRequest) -> None:
    clips = req.manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise HTTPException(400, "manifest.clips must be a non-empty list")
    for c in clips:
        p = c.get("source_path")
        if not isinstance(p, str) or not os.path.isabs(p):
            raise HTTPException(400, f"source_path must be an absolute path: {p!r}")
        if Path(p).suffix.lower() not in VIDEO_EXTENSIONS:
            raise HTTPException(400, f"not a supported video file: {p}")
        try:
            if not float(c["source_in"]) < float(c["source_out"]):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"invalid source_in/source_out for {p}")
    ref = req.options.get("reference")
    if ref and (os.sep in ref or "/" in ref):
        if Path(ref).suffix.lower() not in IMAGE_EXTENSIONS or not Path(ref).is_file():
            raise HTTPException(400, "reference must be an existing image file or a shot id")


def create_app(workdir: Path, allow_origins: list[str] | None = None, token: str | None = None) -> FastAPI:
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    token = token or load_or_create_token(workdir)
    app = FastAPI(title="Consistent Grade", version=__version__)
    app.state.token = token
    if allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=allow_origins, allow_methods=["GET", "POST"],
                           allow_headers=["Content-Type", "X-CG-Token"])
    jobs: dict[str, dict] = {}
    cancels: dict[str, threading.Event] = {}
    allowed = {f.name for f in fields(Options)} - {"cache_dir"}

    def auth(request: Request):
        given = request.headers.get("x-cg-token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(given, token):
            raise HTTPException(401, "missing or wrong service token")

    @app.get("/health")
    def health():
        return {
            "ok": True, "version": __version__,
            "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
            "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }

    @app.post("/ai/check-key", dependencies=[Depends(auth)])
    def ai_check_key(req: KeyCheck):
        return ai.check_key(req.api_key or None)

    @app.post("/analyze", dependencies=[Depends(auth)])
    def analyze(req: AnalyzeRequest):
        unknown = set(req.options) - allowed
        if unknown:
            raise HTTPException(400, f"unknown options: {sorted(unknown)}")
        validate_request(req)
        job_id = uuid.uuid4().hex[:12]
        out = workdir / job_id
        out.mkdir(parents=True)
        (out / "manifest.json").write_text(json.dumps(req.manifest, indent=2))
        job = {"job_id": job_id, "status": "running", "progress": 0.0, "stage": "queued", "log": []}
        jobs[job_id] = job
        cancels[job_id] = threading.Event()

        def set_progress(f, msg):
            job["progress"], job["stage"] = round(f, 3), msg

        def scrub(text: str) -> str:
            return text.replace(req.api_key, "[api key]") if req.api_key else text

        def work():
            try:
                opts = Options(**req.options, cache_dir=str(workdir / ".cache"))
                if opts.ai_mode == "api" and not (req.api_key or os.environ.get("ANTHROPIC_API_KEY")):
                    raise RuntimeError("Claude analysis needs an API key: enter one in the panel or set ANTHROPIC_API_KEY")
                shots = shots_from_manifest(req.manifest, out)
                project = run(shots, out, opts, log=lambda m: job["log"].append(scrub(m)),
                              api_key=req.api_key or None, progress=set_progress,
                              should_cancel=cancels[job_id].is_set)
                job["summary"] = _summary(project, job_id, out)
                job["status"] = "done"
            except Cancelled:
                job["status"] = "cancelled"
            except Exception as e:  # surfaced to the panel, minus any secret
                job["status"] = "error"
                job["error"] = scrub(f"{type(e).__name__}: {e}")
                job["log"].append(scrub(traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()
        return {"job_id": job_id}

    def get_job(job_id: str) -> dict:
        if job_id not in jobs:
            raise HTTPException(404, "unknown job")
        return jobs[job_id]

    @app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
    def job_status(job_id: str):
        return get_job(job_id)

    @app.post("/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    def job_cancel(job_id: str):
        get_job(job_id)
        cancels[job_id].set()
        return {"ok": True}

    @app.post("/jobs/{job_id}/approvals", dependencies=[Depends(auth)])
    def job_approvals(job_id: str, approvals: dict[str, bool]):
        job = get_job(job_id)
        if job["status"] != "done":
            raise HTTPException(409, "job is not finished")
        known = {s["shot_id"] for s in job["summary"]["shots"]}
        if set(approvals) - known:
            raise HTTPException(400, f"unknown shots: {sorted(set(approvals) - known)}")
        path = workdir / job_id / "approvals.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.update(approvals)
        path.write_text(json.dumps(current, indent=2))
        return current

    app.mount("/results", StaticFiles(directory=str(workdir)), name="results")

    @app.middleware("http")
    async def protect_results(request: Request, call_next):
        # Static files bypass dependencies, so check the token here as well.
        if request.url.path.startswith("/results/"):
            given = request.headers.get("x-cg-token") or request.query_params.get("token") or ""
            if not hmac.compare_digest(given, token) or "/.cache/" in request.url.path or request.url.path.endswith(TOKEN_FILE):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "missing or wrong service token"}, status_code=401)
        return await call_next(request)

    return app


def _summary(project: dict, job_id: str, out: Path) -> dict:
    return {
        "consistency": project["consistency"],
        "look": project["look"]["name"],
        "shots": [{
            "shot_id": s["shot_id"], "name": s["name"], "status": s["status"],
            "clip_id": s.get("premiere", {}).get("clip_id"),
            "needs_review": s.get("grade", {}).get("needs_review"),
            "grade": s.get("grade"), "warnings": s["warnings"],
            "grade_cube": str(out / s["files"]["grade_cube"]) if s.get("files") else None,
            "match_cube": str(out / s["files"]["match_cube"]) if s.get("files") else None,
            "preview": f"/results/{job_id}/{s['previews'][0]}" if s.get("previews") else None,
        } for s in project["shots"]],
        "reference_preview": f"/results/{job_id}/{project['reference']['preview']}",
        "look_cube": str(out / "look" / "look.cube"),
        "report": f"/results/{job_id}/report.html",
    }


def serve(host: str, port: int, workdir: Path, certfile: str | None = None, keyfile: str | None = None,
          allow_origins: list[str] | None = None) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to bind to a non-loopback address")
    app = create_app(workdir, allow_origins)
    print(f"Consistent Grade service on {'https' if certfile else 'http'}://{host}:{port}")
    print(f"Service token (paste into the panel): {app.state.token}")
    uvicorn.run(app, host=host, port=port, ssl_certfile=certfile, ssl_keyfile=keyfile)
