"""Command-line interface: ``python -m autograde analyze ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .grade import LOOKS
from .pipeline import Options, run, shots_from_folder, shots_from_manifest


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autograde", description="Consistent Grade: shot matching + LUT export")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyse clips, derive per-shot grades, write LUTs, previews and a report")
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path, help="folder of clips (one shot per file unless --split-scenes)")
    src.add_argument("--manifest", type=Path, help="clip manifest exported by the Premiere panel")
    a.add_argument("--reference", help="reference image path, or a shot id / file name to match to (default: most typical shot)")
    a.add_argument("--output", type=Path, required=True)
    a.add_argument("--look", default="none", help=f"shared look: {', '.join(LOOKS)} or 'ai'")
    a.add_argument("--ai", dest="ai_mode", choices=["off", "prepare", "api", "file", "mock"], default="off",
                   help="off: numbers only; prepare: write request for manual/Claude Code review; "
                        "api: call the Anthropic API (uploads downscaled frames, paid); file: load guidance json; "
                        "mock: deterministic offline guidance")
    a.add_argument("--ai-guidance", help="guidance json for --ai file (default: <output>/ai/guidance.json)")
    a.add_argument("--ai-model", default=None)
    a.add_argument("--brief", dest="creative_brief", default="", help="creative brief text passed to Claude")
    a.add_argument("--lut-size", type=int, default=33, choices=[17, 33, 65])
    a.add_argument("--assume-rec709", action="store_true", help="accept clips with missing colour metadata as Rec.709")
    a.add_argument("--split-scenes", action="store_true", help="detect cuts inside each file and grade each shot")
    a.add_argument("--neutral-target", choices=["reference", "neutral"], default="reference",
                   help="match neutrals to the reference's neutrals, or to true grey")
    a.add_argument("--max-samples", dest="samples_cap", type=int, default=12)
    a.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "consistent-grade"),
                   help="decoded-frame cache ('' to disable)")
    a.add_argument("--json", action="store_true", help="print project.json summary to stdout")

    w = sub.add_parser("worker", help="run the file-based engine used by the Premiere panel")
    w.add_argument("--home", type=Path, default=Path.home() / "ConsistentGrade")
    w.add_argument("--once", action="store_true", help="process pending jobs and exit")

    s = sub.add_parser("serve", help="run the local HTTP service used by the Premiere panel")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--workdir", type=Path, default=Path.home() / "ConsistentGrade")
    s.add_argument("--certfile", help="TLS certificate (Premiere on macOS may refuse plain http)")
    s.add_argument("--keyfile")
    s.add_argument("--allow-origin", action="append", default=[],
                   help="CORS origin to allow (repeatable); none by default")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "worker":
        from .worker import serve_folder
        serve_folder(args.home, once=args.once)
        return 0
    if args.cmd == "serve":
        from .service import serve
        serve(args.host, args.port, args.workdir, args.certfile, args.keyfile, args.allow_origin)
        return 0

    opts = Options(
        reference=args.reference, look=args.look, ai_mode=args.ai_mode, ai_guidance=args.ai_guidance,
        creative_brief=args.creative_brief, lut_size=args.lut_size, assume_rec709=args.assume_rec709,
        split_scenes=args.split_scenes, neutral_target=args.neutral_target, samples_cap=args.samples_cap,
        cache_dir=args.cache_dir or None,
    )
    if args.ai_model:
        opts.ai_model = args.ai_model
    log = (lambda m: print(m, file=sys.stderr))
    if args.input:
        shots = shots_from_folder(args.input, opts)
    else:
        manifest = json.loads(args.manifest.read_text())
        shots = shots_from_manifest(manifest, args.manifest.parent)
    if not shots:
        print("no clips found", file=sys.stderr)
        return 2
    sources = {Path(s.source).resolve() for s in shots}
    if args.output.resolve() in sources:
        print("--output must be a folder, not one of the input files", file=sys.stderr)
        return 2
    project = run(shots, args.output, opts, log=log)
    if args.json:
        print(json.dumps({"consistency": project["consistency"],
                          "shots": [{"shot_id": s["shot_id"], "status": s["status"], "grade": s.get("grade")} for s in project["shots"]]},
                         indent=2))
    return 0
