"""File-based job worker for the Premiere panel (no network needed).

Premiere's UXP blocks ``http://`` on macOS, so the panel and the engine talk
through a folder instead::

    ~/ConsistentGrade/
      worker.json                 heartbeat: {"pid", "version", "time", "ffmpeg"}
      jobs/<job_id>/
        request.json              written by the panel: {"manifest", "options"}
        status.json               written here: status, progress, stage, log, summary
        cancel                    created by the panel to cancel
        ai/request.json           written here when the panel should ask Claude
        ai/guidance.json          written by the panel (Claude's answer), or
        ai/failed.json            written by the panel if the Claude call failed
        approvals.json            written by the panel

The Anthropic API key never reaches this process or the disk: the panel calls
Claude itself, using the images and prompt this worker prepares.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from dataclasses import fields
from pathlib import Path

from . import __version__, ai
from .pipeline import Cancelled, Options, run, shots_from_manifest

POLL_S = 1.0
AI_TIMEOUT_S = 15 * 60


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)  # atomic: the panel never reads a half-written file


def ensure_ffmpeg() -> bool:
    """Use ffmpeg from PATH; fall back to the static-ffmpeg package's binaries."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if (Path(extra) / "ffmpeg").exists():
            os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    try:
        import static_ffmpeg  # type: ignore

        static_ffmpeg.add_paths()
    except Exception:
        return False
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class PanelProvider(ai.Provider):
    """Hands the Claude request to the panel and waits for its answer."""

    name = "panel"

    def __init__(self, job_dir: Path, cancelled):
        self.job_dir, self.cancelled = job_dir, cancelled

    def guidance(self, ref_image, strips, text, shots):
        ai_dir = self.job_dir / "ai"
        ai_dir.mkdir(exist_ok=True)
        for stale in ("guidance.json", "failed.json"):
            (ai_dir / stale).unlink(missing_ok=True)
        _write_json(ai_dir / "request.json", {
            "model": ai.MODEL,
            "system": ai.SYSTEM,
            "schema": ai.SCHEMA,
            "text": text,
            # Images the panel attaches, in order, each preceded by its label.
            "images": [{"label": "Reference image:", "path": str(ref_image)}]
                      + [{"label": f"Shot {sid} samples:", "path": str(p)} for sid, p in strips],
        })
        deadline = time.time() + AI_TIMEOUT_S
        while time.time() < deadline:
            if self.cancelled():
                raise Cancelled("cancelled by user")
            if (ai_dir / "guidance.json").exists():
                return ai.load(ai_dir / "guidance.json")
            if (ai_dir / "failed.json").exists():
                reason = json.loads((ai_dir / "failed.json").read_text()).get("error", "unknown error")
                raise RuntimeError(f"Claude request failed: {reason}")
            time.sleep(0.5)
        raise RuntimeError("timed out waiting for Claude's answer from the panel")


def _summary(project: dict, job_dir: Path) -> dict:
    return {
        "consistency": project["consistency"],
        "look": project["look"]["name"],
        "ai_look": (project.get("ai") or {}).get("look"),
        "reference_preview": str(job_dir / project["reference"]["preview"]),
        "look_cube": str(job_dir / "look" / "look.cube"),
        "report": str(job_dir / "report.html"),
        "shots": [{
            "shot_id": s["shot_id"], "name": s["name"], "status": s["status"],
            "clip_id": s.get("premiere", {}).get("clip_id"),
            "needs_review": (s.get("grade") or {}).get("needs_review"),
            "grade": s.get("grade"), "warnings": s["warnings"],
            "reasons": (s.get("grade") or {}).get("reasons", []),
            "grade_cube": str(job_dir / s["files"]["grade_cube"]) if s.get("files") else None,
            "match_cube": str(job_dir / s["files"]["match_cube"]) if s.get("files") else None,
            "preview": str(job_dir / s["previews"][0]) if s.get("previews") else None,
        } for s in project["shots"]],
    }


def process_job(job_dir: Path, cache_dir: Path) -> dict:
    status = {"status": "running", "progress": 0.0, "stage": "starting", "log": [], "version": __version__}
    status_path = job_dir / "status.json"
    last_write = [0.0]

    def flush(force=False):
        if force or time.time() - last_write[0] > 0.3:
            _write_json(status_path, status)
            last_write[0] = time.time()

    def log(msg):
        status["log"].append(msg)
        flush()

    def progress(f, msg):
        status["progress"], status["stage"] = round(f, 3), msg
        flush()

    cancelled = lambda: (job_dir / "cancel").exists()  # noqa: E731
    flush(True)
    try:
        req = json.loads((job_dir / "request.json").read_text())
        allowed = {f.name for f in fields(Options)} - {"cache_dir", "ai_guidance"}
        options = {k: v for k, v in (req.get("options") or {}).items() if k in allowed}
        use_panel_ai = options.get("ai_mode") == "panel"
        if use_panel_ai:
            options["ai_mode"] = "off"  # the provider below replaces the built-in modes
        opts = Options(**options, cache_dir=str(cache_dir))
        shots = shots_from_manifest(req["manifest"], job_dir)
        provider = PanelProvider(job_dir, cancelled) if use_panel_ai else None
        if provider:
            progress(0.0, "waiting for frames")
        project = run(shots, job_dir, opts, log=log, progress=progress, should_cancel=cancelled,
                      provider=provider)
        status["summary"] = _summary(project, job_dir)
        status["status"] = "done"
    except Cancelled:
        status["status"] = "cancelled"
    except Exception as e:
        status["status"] = "error"
        status["error"] = f"{type(e).__name__}: {e}"
        status["log"].append(traceback.format_exc())
    flush(True)
    return status


def _pending(jobs_dir: Path) -> list[Path]:
    out = []
    for d in sorted(jobs_dir.iterdir(), key=lambda p: p.stat().st_mtime) if jobs_dir.exists() else []:
        if (d / "request.json").exists() and not (d / "status.json").exists():
            out.append(d)
    return out


def recover_interrupted(jobs_dir: Path) -> None:
    """Jobs left 'running' by a previous worker that died are marked as failed."""
    if not jobs_dir.exists():
        return
    for st in jobs_dir.glob("*/status.json"):
        try:
            data = json.loads(st.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == "running":
            data.update(status="error", error="the engine stopped while this job was running; please run it again")
            _write_json(st, data)


def serve_folder(home: Path, once: bool = False) -> None:
    home = home.expanduser()
    jobs_dir = home / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    have_ffmpeg = ensure_ffmpeg()
    recover_interrupted(jobs_dir)
    print(f"Consistent Grade engine {__version__} watching {jobs_dir}")
    print("ffmpeg: " + ("ok" if have_ffmpeg else "NOT FOUND - install it (brew install ffmpeg) and restart"))
    print("Leave this window open while you use the panel. Press Ctrl+C to stop.")
    while True:
        _write_json(home / "worker.json", {"pid": os.getpid(), "version": __version__, "time": time.time(),
                                           "ffmpeg": have_ffmpeg})
        for job in _pending(jobs_dir):
            print(f"job {job.name} ...", flush=True)
            st = process_job(job, home / "cache")
            print(f"job {job.name}: {st['status']} {st.get('error', '')}", flush=True)
            _write_json(home / "worker.json", {"pid": os.getpid(), "version": __version__, "time": time.time(),
                                               "ffmpeg": have_ffmpeg})
        if once:
            return
        time.sleep(POLL_S)
