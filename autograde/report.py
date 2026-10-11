"""Self-contained HTML report (relative links to images in the results folder)."""

from __future__ import annotations

import html
import json
from pathlib import Path


def _e(x) -> str:
    return html.escape(str(x))


def _fmt_delta(d: dict | None) -> str:
    if not d:
        return "—"
    return (f"{d['exposure_ev']:+.2f} EV · R/G×{d['neutral_r_over_g']:.3f} · B/G×{d['neutral_b_over_g']:.3f} · "
            f"contrast {d['contrast_spread']:+.3f} · chroma {d['chroma_p50']:+.3f} · <b>dist {d['distance']:.3f}</b>")


CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e2dc;--warn:#9a5b00;--warnbg:#fff4e0;--ok:#1f6f43}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--muted:#a3a39c;--card:#20201e;--line:#34342f;--warn:#ffb84d;--warnbg:#3a2a10;--ok:#6fd39b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
img{max-width:100%;height:auto;border-radius:6px;display:block}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:8px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.review{background:var(--warnbg);color:var(--warn);padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600}
.okpill{color:var(--ok);font-weight:600;font-size:12px}code{font-size:12px}ul{margin:4px 0 0 18px;padding:0}
.scroll{overflow-x:auto}
"""


def write(path: Path, project: dict) -> None:
    shots = project["shots"]
    c = project["consistency"]
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Consistent Grade Report</title><style>{CSS}</style></head><body><main>",
             "<h1>Consistent Grade report</h1>",
             f"<div class=muted>{_e(project['created'])} · v{_e(project['version'])} · reference: {_e(project['reference']['description'])}"
             f" · look: {_e(project['look']['name'])} · AI: {_e(project['ai']['mode'])}</div>",
             "<div class=muted>Graded from clean source media decoded with FFmpeg: effects, Lumetri or colour "
             "overrides already applied inside Premiere are not seen.</div>"]
    n_review = sum(1 for s in shots if s.get("grade", {}).get("needs_review"))
    n_skip = sum(1 for s in shots if s["status"] != "ok")
    parts.append(
        f"<div class=card><b>{len(shots)}</b> shots · <b>{n_review}</b> need review · <b>{n_skip}</b> skipped · "
        f"mean distance to reference <b>{c['mean_to_reference_before']:.3f} → {c['mean_to_reference_after']:.3f}</b>"
        f" (after matching, before look)</div>")
    if project["ai"].get("look"):
        parts.append(f"<div class=card><b>Claude's look suggestion:</b> {_e(project['ai']['look'].get('preset'))} — "
                     f"{_e(project['ai']['look'].get('rationale', ''))}</div>")
    parts.append("<h2>Contact sheets</h2><div class=card><div class=muted>Before</div>"
                 "<img src='contact_sheet_before.jpg' alt='before'><div class=muted style='margin-top:10px'>After (match + look)</div>"
                 "<img src='contact_sheet_after.jpg' alt='after'></div>")
    parts.append(f"<h2>Reference</h2><div class=card><img src='{_e(project['reference']['preview'])}' alt='reference' style='max-width:480px'></div>")

    parts.append("<h2>Shots</h2>")
    for s in shots:
        g = s.get("grade")
        badge = "<span class=review>needs review</span>" if (g and g["needs_review"]) else (
            "<span class=okpill>ok</span>" if s["status"] == "ok" else f"<span class=review>{_e(s['status'])}</span>")
        parts.append(f"<div class=card><h3 style='margin:0 0 6px'>{_e(s['shot_id'])} · {_e(s['name'])} {badge}</h3>")
        parts.append(f"<div class=muted>{_e(s['source'])} · source {s['source_in']:.3f}–{s['source_out']:.3f}s</div>")
        if s.get("warnings"):
            parts.append("<ul>" + "".join(f"<li>{_e(w)}</li>" for w in s["warnings"]) + "</ul>")
        if s["status"] != "ok":
            parts.append("</div>")
            continue
        parts.append("<div class=scroll><table>"
                     f"<tr><th>vs reference before</th><td>{_fmt_delta(s.get('vs_reference'))}</td></tr>"
                     f"<tr><th>vs reference after</th><td>{_fmt_delta(s.get('after_vs_reference'))}</td></tr>"
                     f"<tr><th>grade</th><td>exposure {g['exposure_ev']:+.3f} EV · RGB {g['rgb_balance']} · contrast {g['contrast']} · "
                     f"saturation {g['saturation']} · confidence {g['confidence']}</td></tr>"
                     f"<tr><th>exposure stability</th><td>{s['measurements']['exposure_variation_ev']:.3f} EV across samples</td></tr>"
                     f"<tr><th>LUT check</th><td>{s['lut_check']['size']}³: max {s['lut_check']['max_code_values']} / mean {s['lut_check']['mean_code_values']} 8-bit code values vs direct transform</td></tr>"
                     f"<tr><th>files</th><td><code>{_e(s['files']['grade_cube'])}</code> · <code>{_e(s['files']['match_cube'])}</code> · <code>{_e(s['files']['grade_json'])}</code></td></tr>"
                     "</table></div>")
        if g["reasons"]:
            parts.append("<div class=muted style='margin-top:6px'>Why:</div><ul>" + "".join(f"<li>{_e(r)}</li>" for r in g["reasons"]) + "</ul>")
        if s.get("ai"):
            parts.append(f"<details><summary>Claude's observations</summary><pre>{_e(json.dumps(s['ai'], indent=1))}</pre></details>")
        ref_img = f"<figure style='margin:0'><img loading=lazy src='{_e(project['reference']['preview'])}' alt='reference'><figcaption class=muted>reference</figcaption></figure>"
        parts.append("<div class=grid style='margin-top:8px'>" + ref_img + "".join(
            f"<figure style='margin:0'><img loading=lazy src='{_e(p)}' alt=''><figcaption class=muted>sample {i + 1}: before | after</figcaption></figure>"
            for i, p in enumerate(s["previews"])) + "</div></div>")

    parts.append("<h2>Applying in Premiere</h2><div class=card>"
                 "<p>Footage must already be Rec.709 SDR in Premiere (no log interpretation, no input LUT, no other grade) — the LUTs expect exactly what was analysed.</p>"
                 "<ol><li>Select the clip → Lumetri Color → <b>Creative › Look</b> → Browse… → choose <code>shot_XXX/grade.cube</code> (match + look in one).</li>"
                 "<li>Or: put <code>shot_XXX/match.cube</code> on each clip and <code>look/look.cube</code> once on an adjustment layer above the sequence.</li>"
                 "<li>Do not also load these as an <i>Input LUT</i> on a clip that already has one.</li></ol></div>")
    parts.append("</main></body></html>")
    path.write_text("\n".join(parts))
