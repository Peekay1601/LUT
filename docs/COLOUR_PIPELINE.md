# Colour pipeline

## Accepted input (MVP)

SDR Rec.709 only, decided from ffprobe metadata per file:

| Field | Accepted | Otherwise |
|---|---|---|
| primaries | `bt709` | rejected |
| transfer | `bt709`, `smpte170m` (same OETF) | rejected (log, PQ, HLG, linear, gamma 2.8…) |
| matrix | `bt709` (decoded as 709); `smpte170m`/`bt470bg` (decoded as 601) | rejected |
| range | `tv` (expanded 16–235 → 0–1) or `pc` | missing → assumed `tv`, recorded as an assumption |

Missing primaries, transfer or matrix make the file **rejected** unless you pass `--assume-rec709`. The
assumption is then recorded in `project.json` and in the report. There is no automatic log detection.

OpenColorIO is not used in the MVP: with a single Rec.709 → Rec.709 path, the only transforms needed are
the BT.709 OETF and its inverse, implemented in `autograde/colour.py`. OCIO becomes worthwhile when log
camera inputs (S-Log3, C-Log, LogC…) are added, as an input-normalisation step ahead of everything below.

## Decoding

FFmpeg converts YUV → RGB with an explicit `in_color_matrix`, `in_range` and `out_range=pc` and pipes
**16-bit** RGB to NumPy (`rgb48le`). Analysis never reads 8-bit PNGs. PNG/JPEG files are written only for
people (and Claude) to look at. Stills used as references are treated as sRGB: they are linearised with
the sRGB EOTF and re-encoded with the Rec.709 OETF.

## Working spaces

* **Signal**: Rec.709 R′G′B′, full-range normalised to [0, 1].
* **Linear**: the inverse BT.709 OETF of the signal (scene-referred), with exact-continuity constants
  α = 1.09929682680944 and β = 0.018053968510807.

## Transform order and parameter meanings

| # | Step | Space | Parameter | Meaning | Bounds |
|---|---|---|---|---|---|
| 1 | Input normalisation | — | — | decode as above (identity for Rec.709) | — |
| 2 | Exposure | linear | `exposure_ev` | multiply linear RGB by 2^ev | ±1.0 EV |
| 2 | White balance | linear | `rgb_balance` | per-channel linear gains, normalised so Rec.709 luminance of grey is unchanged | each 0.90–1.10 |
| 3 | Highlight shoulder | signal | — | only when a gain can push values above 1.0: soft roll-off from 0.88 so the brightest possible value lands on 1.0 (identity otherwise) | — |
| 4 | Contrast | signal | `contrast`, `contrast_pivot` | two-sided power curve: 0→0, pivot→pivot, 1→1, slope = `contrast` at the pivot (default pivot = OETF(0.18) ≈ 0.409) | 0.85–1.15 |
| 5 | Saturation | signal | `saturation` | scale (RGB − Y′) around Rec.709 luma Y′ | 0.80–1.20 |
| 6 | Look: warmth/tint | linear | `warmth`, `tint` | gains (1+w, 1−t, 1−w), luminance-normalised | preset |
| 7 | Look: contrast, saturation | signal | as above | pivot 0.409 | preset |
| 8 | Look: split toning | signal | `shadow_tint`, `highlight_tint` | add tint × (1−Y′)² and tint × Y′² | preset |
| 9 | Look: black lift | signal | `black_lift` | y = lift + (1 − lift) · y | preset |
| 10 | Output | signal | — | clip to [0, 1]; Rec.709 out | — |

Steps 2–5 are the **per-shot match** (`match.cube`). Steps 6–9 are the **shared look** (`look/look.cube`).
`grade.cube` contains both.

## How a match is derived

* **Exposure**: log2 ratio of skin-tone linear luminance when both shots have confident skin. Otherwise
  the ratio of the geometric-mean scene key. If skin and scene key disagree, the two are averaged and
  confidence is lowered. 80% of the difference is corrected, and the correction is halved when the shot
  already clips.
* **Balance**: median linear r/g and b/g of candidate neutral pixels. These are unclipped midtones in the
  lowest chroma band, never the whole-frame average. The correction is weighted by neutral confidence,
  85% of the difference is corrected, and below confidence 0.2 balance is left alone. The target is the
  reference's neutrals (default) or true grey (`--neutral-target neutral`).
* **Contrast and saturation**: re-measured **after** exposure and balance, then 50% of the remaining
  difference is corrected.
* **Guidance** (Claude, mock or hand-written) can only scale a measured correction (preserve = 25%,
  slightly = 80%) or veto it when it points the other way. A veto marks the shot for review.
* **Review flags**: clamped parameters, exposure varying > 0.35 EV inside the shot, low confidence,
  unverifiable casts, and contradicting guidance.

## LUT encoding

* `.cube`, `LUT_3D_SIZE 33` (or 17/65), `DOMAIN 0–1`, red index fastest.
* **Input**: Rec.709 signal, full-range normalised (Premiere's internal 0–1 Rec.709).
  **Output**: the same encoding.
* Every run checks the LUT against the direct transform on the real samples and warns above 2 eight-bit
  code values (in which case use `--lut-size 65`).

## Where the LUT goes in Premiere

* On a clip that Premiere already shows as normal Rec.709 (no log interpretation, no Input LUT):
  **Lumetri Color › Creative › Look › Browse…** → `shot_XXX/grade.cube`.
* Or use `shot_XXX/match.cube` per clip plus `look/look.cube` once on an adjustment layer.
* **Never** put it in Basic Correction › Input LUT on top of another input transform, and never on
  untreated log footage.

## Reference look mode (`--look-match`, panel: Step 2 › Reference look)

Consistency matching is deliberately gentle. When you want a shot to *take on* a stylised reference
(crushed blacks, cool shadows / warm highlights, muted colour) the engine adds a per-shot
**look transfer** after the match (`autograde/transfer.py`), stored in `grade.json` as `look_transfer`
and baked into `grade.cube`:

| Part | How | Bounds |
|---|---|---|
| Tone curve | luma quantiles of the (matched) shot mapped onto the reference's; applied additively (rgb + curve(y) − y) | slope 0.35–2.8, smoothed, monotonic |
| Zone colour | mean chroma of shadows / midtones / highlights moved to the reference's (luma-neutral offsets, smooth zone weights) | ±0.12 per channel; skipped for zones either image barely has |
| Saturation | ratio of median chroma | ×0.55–1.6 |

`strength` (0.5 / 0.75 / 1.0 in the panel) scales all three. It's still one fixed transform per shot
computed from all samples, so it can't flicker. The creative look presets are ignored in this mode (the
reference is the look). Lumetri sliders can't represent it, so for this mode apply the LUT itself.
