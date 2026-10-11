"""End-to-end analysis: shots -> samples -> measurements -> guidance -> grades -> LUTs/previews/report."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import __version__, ai, lut, measure, preview, report
from .grade import LOOKS, Guidance, Look, ShotGrade, apply_grade, derive_grade
from .media import VIDEO_EXTENSIONS, MediaError, MediaInfo, find_inputs, load_image_as_signal, probe, detect_scenes
from .sampling import sample_shot

# Flags from the Premiere manifest that make a clip ineligible in the MVP.
BLOCKING_FLAGS = {"speed_change", "reversed", "nested_sequence", "multicam", "missing_media", "offline",
                  "time_remapping", "unsupported_colour", "existing_grade", "input_lut",
                  "unreadable_media", "unsupported_media"}


@dataclass
class Shot:
    shot_id: str
    source: str
    start: float
    end: float
    name: str = ""
    timeline: dict = field(default_factory=dict)  # timeline in/out etc. from Premiere, informational
    flags: list[str] = field(default_factory=list)
    premiere: dict = field(default_factory=dict)


@dataclass
class Options:
    reference: str | None = None  # image path, or a shot_id / source file name
    look: str = "none"  # preset name, or "ai" to accept Claude's suggestion
    ai_mode: str = "off"  # off | prepare | api | file | mock
    ai_guidance: str | None = None
    ai_model: str = ai.MODEL
    creative_brief: str = ""
    lut_size: int = 33
    assume_rec709: bool = False
    split_scenes: bool = False
    neutral_target: str = "reference"
    samples_cap: int = 12
    analysis_width: int = 960
    cache_dir: str | None = None  # decoded-frame cache; None disables


class Cancelled(RuntimeError):
    pass


def shots_from_folder(folder: Path, opts: Options) -> list[Shot]:
    shots: list[Shot] = []
    for path in find_inputs(folder):
        try:
            info = probe(path, opts.assume_rec709)
        except MediaError:
            shots.append(Shot(f"shot_{len(shots) + 1:03d}", str(path), 0.0, 0.0, name=path.name, flags=["unreadable_media"]))
            continue
        ranges = detect_scenes(info) if opts.split_scenes else [(0.0, info.duration)]
        for a, b in ranges:
            shots.append(Shot(f"shot_{len(shots) + 1:03d}", str(path), a, b, name=path.name if len(ranges) == 1 else f"{path.name} @{a:.2f}s"))
    return shots


def shots_from_manifest(manifest: dict, base: Path) -> list[Shot]:
    shots = []
    for i, c in enumerate(manifest.get("clips", []), 1):
        src = Path(c["source_path"])
        if not src.is_absolute():
            src = base / src
        flags = list(c.get("flags", []))
        if not src.is_file():
            flags.append("missing_media")
        elif src.suffix.lower() not in VIDEO_EXTENSIONS:
            flags.append("unsupported_media")
        shots.append(Shot(
            shot_id=c.get("shot_id") or f"shot_{i:03d}",
            source=str(src),
            start=float(c["source_in"]),
            end=float(c["source_out"]),
            name=c.get("name", src.name),
            timeline={k: c.get(k) for k in ("timeline_in", "timeline_out", "track_index") if k in c},
            flags=flags,
            premiere={k: c.get(k) for k in ("clip_id", "existing_effects", "speed") if k in c},
        ))
    return shots


def _rel(p: Path, root: Path) -> str:
    return str(p.relative_to(root))


def run(shots: list[Shot], out: Path, opts: Options, log: Callable[[str], None] = print,
        api_key: str | None = None, progress: Callable[[float, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None, provider: "ai.Provider | None" = None) -> dict:
    """Run the analysis.

    ``api_key`` is used only for the Claude call and is never written to disk.
    ``progress(fraction, message)`` is called as work advances; ``should_cancel()``
    is polled between steps and aborts with ``Cancelled``.
    """
    t0 = time.time()
    progress = progress or (lambda f, m: None)

    def check():
        if should_cancel and should_cancel():
            raise Cancelled("cancelled by user")

    out.mkdir(parents=True, exist_ok=True)
    infos: dict[str, MediaInfo] = {}
    results: list[dict] = []
    samples_by_shot: dict[str, list] = {}

    # ---------------- sample + measure ----------------
    for n_shot, shot in enumerate(shots):
        check()
        progress(0.55 * n_shot / max(len(shots), 1), f"sampling {shot.shot_id}")
        rec: dict = {"shot_id": shot.shot_id, "name": shot.name, "source": shot.source,
                     "source_in": round(shot.start, 4), "source_out": round(shot.end, 4),
                     "timeline": shot.timeline, "premiere": shot.premiere,
                     "flags": list(shot.flags), "warnings": [], "status": "ok"}
        results.append(rec)
        blocking = sorted(set(shot.flags) & BLOCKING_FLAGS)
        if blocking:
            rec["status"] = "skipped"
            rec["warnings"].append("unsupported in MVP: " + ", ".join(blocking))
            log(f"{shot.shot_id}: skipped ({', '.join(blocking)})")
            continue
        try:
            info = infos.get(shot.source) or probe(Path(shot.source), opts.assume_rec709)
        except MediaError as e:
            rec["status"] = "skipped"
            rec["warnings"].append(str(e))
            continue
        infos[shot.source] = info
        rec["media"] = info.to_dict()
        if not info.interpretation.supported:
            rec["status"] = "skipped"
            rec["flags"].append("unsupported_colour")
            rec["warnings"] += info.interpretation.problems
            log(f"{shot.shot_id}: skipped ({'; '.join(info.interpretation.problems)})")
            continue
        rec["warnings"] += info.interpretation.assumed
        end = min(shot.end, info.duration) if info.duration else shot.end
        if shot.start >= end:
            rec["status"] = "skipped"
            rec["warnings"].append(f"source in ({shot.start:.3f}s) is not before source out ({end:.3f}s)")
            continue
        try:
            samples, warns = sample_shot(info, shot.start, end, width=opts.analysis_width, cap=opts.samples_cap,
                                         cache_dir=Path(opts.cache_dir) if opts.cache_dir else None)
        except MediaError as e:  # corrupt/truncated media: skip the shot, keep going
            rec["status"] = "skipped"
            rec["warnings"].append(str(e))
            log(f"{shot.shot_id}: skipped ({e})")
            continue
        rec["warnings"] += warns
        if not samples:
            rec["status"] = "skipped"
            continue
        shot_dir = out / shot.shot_id
        frames = []
        for k, s in enumerate(samples):
            p = preview.save_signal(s.signal, shot_dir / "samples" / f"sample_{k + 1:02d}_{s.time:09.3f}s.png")
            m = measure.measure_frame(s.signal)
            frames.append({"time": round(s.time, 4), "file": _rel(p, out), "quality": s.quality, "measurements": m})
        rec["samples"] = frames
        rec["measurements"] = measure.aggregate([f["measurements"] for f in frames])
        samples_by_shot[shot.shot_id] = samples
        log(f"{shot.shot_id}: {len(samples)} samples, key {rec['measurements']['log_key_linear']:.4f}")

    analysed = [r for r in results if r["status"] == "ok"]
    if not analysed:
        raise RuntimeError("no shots could be analysed; see warnings in project.json")

    # ---------------- reference ----------------
    ref_desc, ref_meas, ref_shot_id, ref_preview = _resolve_reference(opts.reference, analysed, samples_by_shot, out)
    for r in analysed:
        r["vs_reference"] = measure.compare(ref_meas, r["measurements"])

    # ---------------- sheets (before) ----------------
    sheet_rows = [(r["shot_id"], [(f"{s.time:.1f}s", s.signal) for s in samples_by_shot[r["shot_id"]]]) for r in analysed]
    preview.contact_sheet(sheet_rows, out / "contact_sheet_before.jpg")
    strips = []
    for label, frames in sheet_rows:
        strips.append((label, preview.contact_sheet([(label, frames)], out / label / "strip.jpg")))

    # ---------------- AI guidance ----------------
    guidance_doc = None
    if provider is None:
        provider = ai.get_provider(opts.ai_mode, out, api_key=api_key, model=opts.ai_model, guidance_path=opts.ai_guidance)
    if provider is not None:
        check()
        progress(0.6, f"AI guidance ({provider.name})")
        text = ai.build_prompt_text(analysed, ref_meas, opts.creative_brief)
        guidance_doc = provider.guidance(ref_preview, strips, text, analysed)
        if provider.name == "prepare":
            log(f"AI request written to {out / 'ai' / 'request.md'}; write ai/guidance.json and rerun with --ai file")
        elif guidance_doc is not None:
            (out / "ai").mkdir(exist_ok=True)
            (out / "ai" / f"guidance_{provider.name}.json").write_text(json.dumps(guidance_doc, indent=2))
    by_id = {g["shot_id"]: g for g in (guidance_doc or {}).get("shots", [])}

    # ---------------- look ----------------
    look_name = opts.look
    if look_name == "ai":
        look_name = (guidance_doc or {}).get("look", {}).get("preset", "none")
    if look_name not in LOOKS:
        raise ValueError(f"unknown look '{look_name}'. Choose from: {', '.join(LOOKS)} or 'ai'")
    look: Look = LOOKS[look_name]
    look_dir = out / "look"
    look_dir.mkdir(exist_ok=True)
    (look_dir / "look.json").write_text(json.dumps(look.to_dict(), indent=2))
    lut.write_cube(look_dir / "look.cube", lut.build_lut(None, look, opts.lut_size), opts.lut_size,
                   f"look {look.name}", ["Shared creative look. Rec.709 signal in -> Rec.709 signal out."])

    # ---------------- grades, LUTs, previews ----------------
    for n_shot, r in enumerate(analysed):
        check()
        progress(0.65 + 0.3 * n_shot / len(analysed), f"grading {r['shot_id']}")
        sid = r["shot_id"]
        shot_dir = out / sid
        g_ai = by_id.get(sid)
        if g_ai:
            r["ai"] = g_ai
        if sid == ref_shot_id:
            grade = ShotGrade(shot_id=sid, reasons=["reference shot: no matching correction"])
        else:
            sigs = [s.signal for s in samples_by_shot[sid]]
            grade = derive_grade(
                sid, r["measurements"], ref_meas, Guidance.from_ai(g_ai) if g_ai else None, opts.neutral_target,
                remeasure=lambda partial, sigs=sigs: measure.aggregate([measure.measure_frame(apply_grade(x, partial)) for x in sigs]),
            )
        if r["warnings"] and any("assumed" in w for w in r["warnings"]):
            grade.reasons.append("colour metadata partly assumed")
        r["grade"] = grade.to_dict()
        (shot_dir / "grade.json").write_text(json.dumps(grade.to_dict(), indent=2))
        (shot_dir / "measurements.json").write_text(json.dumps(
            {"shot": r["measurements"], "frames": r["samples"], "vs_reference": r["vs_reference"]}, indent=2))

        notes = [f"shot {sid} ({r['name']})", "Rec.709 signal in -> Rec.709 signal out (full-range normalised).",
                 f"exposure {grade.exposure_ev:+.3f} EV, rgb {grade.rgb_balance}, contrast {grade.contrast}, saturation {grade.saturation}"]
        lut.write_cube(shot_dir / "match.cube", lut.build_lut(grade, None, opts.lut_size), opts.lut_size, f"{sid} match", notes)
        lut.write_cube(shot_dir / "grade.cube", lut.build_lut(grade, look, opts.lut_size), opts.lut_size,
                       f"{sid} match+{look.name}", notes + [f"includes look '{look.name}'"])

        after_meas, previews, errs = [], [], []
        for k, s in enumerate(samples_by_shot[sid]):
            matched = apply_grade(s.signal, grade, None)
            graded = apply_grade(s.signal, grade, look)
            after_meas.append(measure.measure_frame(matched))
            previews.append(_rel(preview.before_after(s.signal, graded, shot_dir / "previews" / f"ba_{k + 1:02d}.jpg"), out))
            errs.append(lut.lut_error(s.signal, grade, look, opts.lut_size))
        r["previews"] = previews
        r["after"] = measure.aggregate(after_meas)
        r["after_vs_reference"] = measure.compare(ref_meas, r["after"])
        worst = max(e["max_code_values"] for e in errs)
        r["lut_check"] = {"size": opts.lut_size, "max_code_values": worst,
                          "mean_code_values": round(float(np.mean([e["mean_code_values"] for e in errs])), 4)}
        if worst > 2.0:
            r["warnings"].append(f"LUT interpolation error up to {worst:.1f} code values; consider --lut-size 65")
        r["files"] = {"grade_json": f"{sid}/grade.json", "match_cube": f"{sid}/match.cube",
                      "grade_cube": f"{sid}/grade.cube", "strip": f"{sid}/strip.jpg"}

    # ---------------- after sheet + consistency ----------------
    preview.contact_sheet(
        [(r["shot_id"], [(f"{s.time:.1f}s", apply_grade(s.signal, ShotGrade.from_dict(r["grade"]), look))
                         for s in samples_by_shot[r["shot_id"]]]) for r in analysed],
        out / "contact_sheet_after.jpg")
    consistency = _consistency(analysed)

    project = {
        "tool": "consistent-grade", "version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "elapsed_s": round(time.time() - t0, 2),
        "colour_pipeline": {
            "working": "Rec.709 signal, full-range normalised [0,1]; exposure/balance in scene-linear via inverse BT.709 OETF",
            "lut_domain": "Rec.709 signal in -> Rec.709 signal out",
            "order": ["exposure+balance (linear)", "highlight shoulder", "contrast", "saturation", "look"],
        },
        "options": {k: v for k, v in vars(opts).items()},
        "reference": {"description": ref_desc, "shot_id": ref_shot_id, "measurements": ref_meas,
                      "preview": _rel(ref_preview, out)},
        "look": look.to_dict(),
        "ai": {"mode": provider.name if provider else "off", "look": (guidance_doc or {}).get("look")},
        "consistency": consistency,
        "shots": results,
        "files": {"contact_before": "contact_sheet_before.jpg", "contact_after": "contact_sheet_after.jpg",
                  "look_cube": "look/look.cube", "report": "report.html"},
    }
    (out / "project.json").write_text(json.dumps(project, indent=2, default=_json_default))
    report.write(out / "report.html", project)
    progress(1.0, "done")
    log(f"done in {project['elapsed_s']} s -> {out / 'report.html'}")
    return project


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def _resolve_reference(ref: str | None, analysed: list[dict], samples_by_shot: dict, out: Path):
    if ref:
        p = Path(ref)
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"} and p.exists():
            sig = load_image_as_signal(p)
            m = measure.aggregate([measure.measure_frame(sig)])
            return f"image {p.name}", m, None, preview.save_signal(sig, out / "reference.png")
        for r in analysed:
            if ref in (r["shot_id"], Path(r["source"]).name, r["name"]):
                sid = r["shot_id"]
                break
        else:
            raise ValueError(f"reference '{ref}' is neither an image file nor an analysed shot")
    else:
        # No reference given: pick the most "typical" shot (smallest total distance to the others).
        def total(r):
            return sum(measure.compare(r["measurements"], o["measurements"])["distance"] for o in analysed)
        sid = min(analysed, key=total)["shot_id"]
    r = next(x for x in analysed if x["shot_id"] == sid)
    mid = samples_by_shot[sid][len(samples_by_shot[sid]) // 2]
    return f"shot {sid} ({r['name']})", r["measurements"], sid, preview.save_signal(mid.signal, out / "reference.png")


def _consistency(analysed: list[dict]) -> dict:
    def series(key):
        vals = [r[key] for r in analysed]
        adj = [measure.compare(a, b)["distance"] for a, b in zip(vals, vals[1:])]
        return adj
    before = series("measurements")
    after = series("after")
    to_ref_before = [r["vs_reference"]["distance"] for r in analysed]
    to_ref_after = [r["after_vs_reference"]["distance"] for r in analysed]
    return {
        "adjacent_distance_before": before, "adjacent_distance_after": after,
        "to_reference_before": to_ref_before, "to_reference_after": to_ref_after,
        "mean_to_reference_before": round(float(np.mean(to_ref_before)), 4),
        "mean_to_reference_after": round(float(np.mean(to_ref_after)), 4),
    }
