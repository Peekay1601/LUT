"""Local, numerical image measurements.

All functions take Rec.709 signal arrays (see ``colour.py``). Nothing here
derives white balance from whole-frame averages: colour bias is estimated only
from candidate neutral pixels and reported with a confidence, so a forest or a
red studio wall does not get "corrected" to grey.
"""

from __future__ import annotations

import cv2
import numpy as np

from . import colour

PCTS = (1, 5, 10, 25, 50, 75, 90, 95, 99)


def _pcts(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {f"p{p}": float("nan") for p in PCTS}
    q = np.percentile(values, PCTS)
    return {f"p{p}": round(float(v), 5) for p, v in zip(PCTS, q)}


def frame_quality(sig: np.ndarray) -> dict:
    """Cheap per-frame checks used to reject unusable samples."""
    y = colour.luma(sig)
    small = cv2.resize(y, (320, max(2, int(320 * y.shape[0] / y.shape[1]))), interpolation=cv2.INTER_AREA)
    sharp = float(cv2.Laplacian(small, cv2.CV_32F).var())
    return {
        "mean_luma": float(y.mean()),
        "p95_luma": float(np.percentile(y, 95)),
        "luma_std": float(y.std()),
        "sharpness": sharp,
        "is_black": bool(np.percentile(y, 95) < 0.06),
        "is_flat": bool(y.std() < 0.01),
    }


def neutral_bias(sig: np.ndarray) -> dict:
    """Estimate the colour bias of things that are probably neutral.

    Candidates are unclipped midtone pixels whose chroma is in the lowest part of
    the frame's chroma distribution *and* below an absolute ceiling. The bias is
    the median linear r/g and b/g of those pixels. Confidence grows with the
    number of candidates and shrinks when the candidates disagree.
    """
    lin = colour.inverse_oetf_709(sig)
    y = colour.luma(sig)
    c = colour.chroma(sig)
    unclipped = (sig.max(axis=-1) < 0.97) & (sig.min(axis=-1) > 0.03)
    mid = (y > 0.18) & (y < 0.85) & unclipped
    if mid.sum() < 50:
        return {"r_over_g": 1.0, "b_over_g": 1.0, "fraction": 0.0, "confidence": 0.0}
    ceiling = min(0.22, float(np.percentile(c[mid], 15)) + 0.04)
    cand = mid & (c <= ceiling)
    frac = float(cand.mean())
    if cand.sum() < 50:
        return {"r_over_g": 1.0, "b_over_g": 1.0, "fraction": frac, "confidence": 0.0}
    px = lin[cand]
    g = np.maximum(px[:, 1], 1e-5)
    rg = px[:, 0] / g
    bg = px[:, 2] / g
    r_med, b_med = float(np.median(rg)), float(np.median(bg))
    # Spread of candidates in log space: tight cluster -> believable neutral.
    spread = float(np.median(np.abs(np.log(rg / r_med))) + np.median(np.abs(np.log(bg / b_med))))
    conf = min(1.0, frac / 0.04) * float(np.clip(1.0 - spread / 0.25, 0.0, 1.0))
    return {
        "r_over_g": round(r_med, 5),
        "b_over_g": round(b_med, 5),
        "fraction": round(frac, 5),
        "spread": round(spread, 5),
        "confidence": round(conf, 4),
    }


def skin_stats(sig: np.ndarray) -> dict:
    """Very simple skin-tone candidate statistics (YCrCb box), with a confidence.

    This is a heuristic, not a face detector. It is used only to compare shots
    that both contain a believable amount of skin-like colour.
    """
    img8 = (np.clip(sig, 0, 1) * 255).astype(np.uint8)
    ycrcb = cv2.cvtColor(img8, cv2.COLOR_RGB2YCrCb)
    yy, cr, cb = ycrcb[..., 0], ycrcb[..., 1], ycrcb[..., 2]
    mask = (cr >= 135) & (cr <= 172) & (cb >= 85) & (cb <= 127) & (yy > 50) & (yy < 235)
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)).astype(bool)
    frac = float(mask.mean())
    if mask.sum() < 200:
        return {"fraction": frac, "confidence": 0.0}
    lin = colour.inverse_oetf_709(sig[mask])
    y_lin = colour.luma(lin)
    # Confidence: enough area but not "the whole frame is orange".
    conf = float(np.clip(frac / 0.03, 0, 1) * np.clip((0.6 - frac) / 0.3, 0, 1))
    g = np.maximum(lin[:, 1], 1e-5)
    return {
        "fraction": round(frac, 5),
        "median_luma_signal": round(float(np.median(colour.luma(sig[mask]))), 5),
        "median_luminance_linear": round(float(np.median(y_lin)), 6),
        "r_over_g": round(float(np.median(lin[:, 0] / g)), 5),
        "b_over_g": round(float(np.median(lin[:, 2] / g)), 5),
        "confidence": round(conf, 4),
    }


def measure_frame(sig: np.ndarray) -> dict:
    y = colour.luma(sig)
    lin = colour.inverse_oetf_709(sig)
    y_lin = colour.luma(lin)
    c = colour.chroma(sig)
    unclipped = (y > 0.02) & (y < 0.98)
    return {
        "luma": _pcts(y),
        "luma_mean": round(float(y.mean()), 5),
        # Geometric-mean scene key in linear light: robust "how bright" number used for EV.
        "log_key_linear": round(float(np.exp(np.mean(np.log(np.clip(y_lin[unclipped] if unclipped.any() else y_lin, 1e-4, None))))), 6),
        "median_luminance_linear": round(float(np.median(y_lin)), 6),
        "clip_black": round(float((y <= 0.02).mean()), 5),
        "clip_white": round(float((sig.max(axis=-1) >= 0.985).mean()), 5),
        "rgb_median": [round(float(v), 5) for v in np.median(sig.reshape(-1, 3), axis=0)],
        "chroma": _pcts(c[(y > 0.1) & (y < 0.95)] if ((y > 0.1) & (y < 0.95)).any() else c.ravel()),
        "contrast_spread": round(float(np.percentile(y, 90) - np.percentile(y, 10)), 5),
        "neutral": neutral_bias(sig),
        "skin": skin_stats(sig),
    }


def _weighted_median(values: list[float], weights: list[float]) -> float:
    v = np.asarray(values, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    if w.sum() <= 0:
        return float(np.median(v))
    order = np.argsort(v)
    v, w = v[order], w[order]
    cw = np.cumsum(w)
    return float(v[np.searchsorted(cw, cw[-1] / 2.0)])


def aggregate(frames: list[dict]) -> dict:
    """Combine per-frame measurements into robust shot-level statistics."""
    if not frames:
        raise ValueError("no frames to aggregate")
    med = lambda key: float(np.median([f[key] for f in frames]))  # noqa: E731
    keys = np.array([f["log_key_linear"] for f in frames])
    ev_series = np.log2(np.maximum(keys, 1e-6) / np.median(keys))
    neutral_conf = [f["neutral"]["confidence"] for f in frames]
    skin_conf = [f["skin"]["confidence"] for f in frames]
    has_skin = [f for f in frames if f["skin"]["confidence"] > 0]
    out = {
        "frames": len(frames),
        "log_key_linear": round(med("log_key_linear"), 6),
        "luma_p50": round(float(np.median([f["luma"]["p50"] for f in frames])), 5),
        "luma_p10": round(float(np.median([f["luma"]["p10"] for f in frames])), 5),
        "luma_p90": round(float(np.median([f["luma"]["p90"] for f in frames])), 5),
        "luma_p99": round(float(np.median([f["luma"]["p99"] for f in frames])), 5),
        "contrast_spread": round(med("contrast_spread"), 5),
        "chroma_p50": round(float(np.median([f["chroma"]["p50"] for f in frames])), 5),
        "clip_black": round(med("clip_black"), 5),
        "clip_white": round(med("clip_white"), 5),
        # Temporal stability: how much the scene key moves between samples, in stops.
        "exposure_variation_ev": round(float(ev_series.max() - ev_series.min()), 4),
        "neutral": {
            "r_over_g": round(_weighted_median([f["neutral"]["r_over_g"] for f in frames], neutral_conf), 5),
            "b_over_g": round(_weighted_median([f["neutral"]["b_over_g"] for f in frames], neutral_conf), 5),
            "confidence": round(float(np.median(neutral_conf)), 4),
        },
        "skin": {
            "confidence": round(float(np.median(skin_conf)), 4),
        },
    }
    if has_skin:
        out["skin"].update({
            "median_luminance_linear": round(_weighted_median(
                [f["skin"]["median_luminance_linear"] for f in has_skin],
                [f["skin"]["confidence"] for f in has_skin]), 6),
            "r_over_g": round(_weighted_median([f["skin"]["r_over_g"] for f in has_skin],
                                               [f["skin"]["confidence"] for f in has_skin]), 5),
            "b_over_g": round(_weighted_median([f["skin"]["b_over_g"] for f in has_skin],
                                               [f["skin"]["confidence"] for f in has_skin]), 5),
        })
    return out


def compare(a: dict, b: dict) -> dict:
    """Shot-to-shot difference summary (b relative to a)."""
    d = {
        "exposure_ev": round(float(np.log2(b["log_key_linear"] / a["log_key_linear"])), 3),
        "luma_p50": round(b["luma_p50"] - a["luma_p50"], 4),
        "contrast_spread": round(b["contrast_spread"] - a["contrast_spread"], 4),
        "chroma_p50": round(b["chroma_p50"] - a["chroma_p50"], 4),
        "neutral_r_over_g": round(b["neutral"]["r_over_g"] / a["neutral"]["r_over_g"], 4),
        "neutral_b_over_g": round(b["neutral"]["b_over_g"] / a["neutral"]["b_over_g"], 4),
        "neutral_confidence": round(min(a["neutral"]["confidence"], b["neutral"]["confidence"]), 3),
    }
    # A single scalar for "how different": EV + colour + contrast, roughly perceptual weights.
    d["distance"] = round(float(
        abs(d["exposure_ev"])
        + 4.0 * (abs(np.log(d["neutral_r_over_g"])) + abs(np.log(d["neutral_b_over_g"]))) * max(d["neutral_confidence"], 0.2)
        + 3.0 * abs(d["contrast_spread"])
        + 3.0 * abs(d["chroma_p50"])
    ), 4)
    return d
