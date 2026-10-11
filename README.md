# Consistent Grade

Shot matching for Premiere Pro. The tool samples representative frames from each clip and measures
exposure, neutral balance, contrast and saturation locally. Claude can optionally judge which differences
are intentional. The tool then derives **one bounded, stable grade per shot** and exports `.cube` LUTs,
before/after previews and an HTML report. A UXP panel reads your timeline selection and drives all of it.

> **Status: MVP.** The grading engine, CLI, local service and panel are built and tested against synthetic
> footage (`pytest`: 32 tests). Premiere integration is **not yet verified on a real install**. See
> [docs/CAPABILITIES.md](docs/CAPABILITIES.md) and run [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).
> LUTs are applied **manually** in Lumetri because no documented Premiere API assigns LUT files.

```
Premiere panel (UXP)  ──manifest──▶  local service (FastAPI, 127.0.0.1, token)
  read selection                        FFmpeg 16-bit decode, explicit matrix/range
  reference / look / API key            NumPy + OpenCV measurements
  progress, cancel, approve             Claude guidance (optional, bounded)
  copy LUT path → Lumetri               grade engine → .cube LUTs, previews, report
```

## Install

Requires Python 3.10+ and FFmpeg/ffprobe on `PATH`.

```bash
pip install -e .            # or: pip install -r requirements.txt
```

## 1. Command line (start here)

```bash
python -m autograde analyze \
  --input ./footage \
  --reference ./reference.jpg \        # or a clip file name / shot id; default = most typical shot
  --output ./results \
  --look none                          # none | natural_commercial | warm_documentary | muted_cinematic | clean_product | ai
```

Outputs:

```
results/
  project.json                 everything: options, media info, measurements, grades, warnings
  report.html                  all shots beside the reference, before/after, reasons, LUT checks
  contact_sheet_before.jpg / contact_sheet_after.jpg
  reference.png
  look/look.cube, look.json    shared creative look
  shot_001/
    samples/                   individual 8-bit PNGs for viewing (analysis uses 16-bit decodes)
    previews/                  before | after for each sample
    strip.jpg                  this shot's samples, as sent to Claude
    measurements.json
    grade.json                 the parameter recipe
    match.cube                 shot match only
    grade.cube                 match + look (33³)
```

Useful flags: `--assume-rec709` (untagged files), `--split-scenes` (several shots in one file),
`--lut-size 65`, `--neutral-target neutral`, `--max-samples 12`, `--cache-dir ''`.

Try it with synthetic footage that has known errors:

```bash
python -m tests.fixtures ./footage
python -m autograde analyze --input ./footage --reference a_reference.mp4 --output ./results
open results/report.html
```

## 2. Claude

Claude only gives **directions** (for example `increase_slightly`, `preserve`, `reduce_green_slightly`)
plus a look preset. The engine turns them into bounded numbers. Claude never writes LUT values.

| `--ai` | What happens |
|---|---|
| `off` (default) | measurements only |
| `prepare` | writes `results/ai/request.md` + `schema.json` for a person or Claude Code to review offline; then `--ai file` |
| `api` | sends downscaled sample strips and the reference to the Anthropic API (`claude-opus-5-5`); **uploads images and costs money** |
| `file` | loads `results/ai/guidance.json` (or `--ai-guidance path`) |
| `mock` | deterministic offline guidance, for tests and dry runs |

With the CLI, set `ANTHROPIC_API_KEY`. In the panel, paste your key under **Claude API key**. It is kept in
UXP secure storage, sent only to the local service over loopback with each analysis, and never written to
disk or logs. **Test key** checks it with a free model lookup.

## 3. Premiere panel

Requires **Premiere Pro 25.6+** (UXP).

1. Start the service: `python -m autograde serve`. It prints a **service token**.
   On macOS Premiere may refuse plain `http://`. If the panel can't connect, create a localhost
   certificate (for example with `mkcert 127.0.0.1`) and run
   `python -m autograde serve --certfile 127.0.0.1.pem --keyfile 127.0.0.1-key.pem`, then change
   `SERVICE` in `premiere-panel/main.js` to `https://`.
2. Load `premiere-panel/` with Adobe **UXP Developer Tool** (Add Plugin → `manifest.json` → Load).
3. In the panel, paste the service token and your Claude API key.
4. Select clips on the timeline → **Read selected clips**. Clips with speed changes, reverse, nesting,
   offline media, an Input LUT or an existing Lumetri grade are flagged and skipped.
5. Pick a reference (a selected clip or an image), pick a look, and decide whether frames may go to Claude.
6. **Analyse & build LUTs** → previews, needs-review badges, **Copy LUT path**, **Approved**.
7. Apply each LUT: Lumetri Color → Creative → Look → Browse… → paste the path.

The panel never modifies your sequence. Diagnostics: **Run capability test** reports what your Premiere
version actually supports. **Inspect Lumetri params** lists a Lumetri instance's parameters (read-only).

You can also use **Save manifest…** and run the CLI against it:
`python -m autograde analyze --manifest manifest.json --output results`.

## What this does not do (yet)

* HDR, RAW, log footage, automatic log detection, multicam, speed ramps, nested sequences.
* Time-varying corrections. Shots whose exposure changes are flagged instead of keyframed.
* Masks, relighting, noise reduction or subject-only fixes. A LUT can't represent these.
* Grading what Premiere *displays*. Analysis reads clean source media (see the capability matrix).
* Automatic LUT application in Premiere.

## Development

```bash
pytest -q
```

Docs: [colour pipeline](docs/COLOUR_PIPELINE.md) · [capability matrix](docs/CAPABILITIES.md) ·
[plan & milestones](docs/PLAN.md) · [acceptance test](docs/ACCEPTANCE.md)
