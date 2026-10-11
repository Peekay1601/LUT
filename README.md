# Consistent Grade

Shot matching for Premiere Pro. The panel reads the clips you select and samples representative frames.
It measures exposure, neutral balance, contrast and saturation on your Mac, and can optionally ask Claude
which differences are intentional. It then builds **one stable, bounded grade per shot** as a `.cube`
LUT, with before/after previews and a report.

## Install on macOS (Premiere Pro 25.6 or later)

1. Download **[`dist/ConsistentGrade.ccx`](dist/ConsistentGrade.ccx)** and **double-click it**.
   Creative Cloud installs it. Because it isn't from the Adobe Marketplace, you'll see a warning:
   choose Install.
2. Open Premiere → **Window › UXP Plugins › Consistent Grade**.
3. In the panel, click **Start engine**. Allow the prompt, and Terminal opens. The **first run takes a
   few minutes**: it installs a private Python, numpy, OpenCV, Pillow and FFmpeg (via Homebrew if you
   have it, otherwise a static build). Later starts take seconds. **Keep that Terminal window open**
   while you work.
4. *(Optional)* Paste your Claude API key → **Test key** → **Save key**.

Then: select clips on the timeline → **Read selected clips** → choose a reference and a look →
**Analyse & build LUTs** → for each shot, **Copy LUT path** → Lumetri Color › Creative › Look ›
Browse… → ⌘⇧G → paste.

The panel never changes your sequence. Clips with speed changes, reverse playback, nesting, offline
media, an Input LUT or an existing Lumetri grade are flagged and skipped.

**Privacy:** without "Ask Claude" ticked, nothing leaves your Mac. With it, only small still images
and the measurements go to the Anthropic API, never video. See [docs/PRIVACY.md](docs/PRIVACY.md).

### How the pieces talk

```
Premiere panel (UXP)                        Engine (Python, started from the panel)
  reads timeline selection                    FFmpeg 16-bit decode, explicit matrix/range
  writes ~/ConsistentGrade/jobs/<id>/ ──────▶  measurements → bounded grades → LUTs, report
  calls api.anthropic.com (your key) ◀──────   asks the panel to consult Claude when enabled
  shows previews, copy LUT path, approve
```

Premiere blocks `http://` on macOS, so the panel and the engine exchange job files instead of using a
local server. Your API key stays inside Premiere; the engine never sees it.

## Status

Built and tested outside Premiere:
* 37 Python tests on synthetic footage with known exposure and colour errors.
* A harness that runs the real panel code against fake Premiere APIs and the real engine
  ([tests/panel](tests/panel/README.md)).

**Not yet verified inside Premiere 26.5:** run [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) and the
panel's **Run capability test**. LUTs are applied manually because no documented Premiere API assigns
LUT files ([capability matrix](docs/CAPABILITIES.md)).

## Command line (no Premiere needed)

```bash
pip install -e .
python -m autograde analyze --input ./footage --reference ./reference.jpg --output ./results \
  --look none        # none | natural_commercial | warm_documentary | muted_cinematic | clean_product | ai
```

Outputs per shot: `samples/`, `previews/`, `measurements.json`, `grade.json`, `match.cube`,
`grade.cube` (match + look). Shared files: `project.json`, `report.html`, contact sheets and
`look/look.cube`.

Claude from the CLI (`--ai`): `off` (default) · `prepare` (writes the request for offline review) ·
`api` (uses `ANTHROPIC_API_KEY`; uploads small stills; paid) · `file` · `mock` (deterministic, offline).
Other flags: `--assume-rec709`, `--split-scenes`, `--lut-size 65`, `--neutral-target neutral`.

Try it with synthetic footage:

```bash
python -m tests.fixtures ./footage
python -m autograde analyze --input ./footage --reference a_reference.mp4 --output ./results
```

## Scope

SDR Rec.709 only: log, HDR and RAW are rejected. There are no time-varying corrections; shots whose
exposure changes are flagged instead. No masks, relighting or noise reduction, because a LUT can't
represent them. Analysis reads clean source media, not what Premiere currently displays.

## Development

```bash
pytest -q                          # Python tests
python packaging/build_ccx.py      # rebuild dist/ConsistentGrade.ccx
```

Docs: [colour pipeline](docs/COLOUR_PIPELINE.md) · [capabilities](docs/CAPABILITIES.md) ·
[plan](docs/PLAN.md) · [acceptance test](docs/ACCEPTANCE.md) · [privacy](docs/PRIVACY.md)
