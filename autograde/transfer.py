"""Reference look transfer: make a shot take on the *look* of a reference.

Shot matching (``grade.py``) deliberately makes small, global corrections so
intentional lighting survives. When the goal is instead "make it look like this
frame" (crushed blacks, blue shadows, orange highlights...), this module derives
a stronger but still smooth, fixed, per-shot transform:

1. **Tone curve** on luma: maps the shot's luma distribution onto the
   reference's (quantile matching), slope-limited and blended by strength.
   Applied additively (rgb + curve(y) - y) so lifting shadows doesn't explode
   their saturation.
2. **Zone colour offsets**: the average colour (chroma, luma removed) of
   shadows, midtones and highlights is moved towards the reference's. This is
   what reproduces split toning.
3. **Saturation** ratio between reference and shot.

Everything works on Rec.709 signal (see ``colour.py``), is computed once per
shot from all of its samples (so it can't flicker), and is bounded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from . import colour

QUANTILES = np.array([0.5, 2, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 98, 99.5])
CURVE_POINTS = 33
SLOPE_RANGE = (0.35, 2.8)
MAX_ZONE_OFFSET = 0.12
SAT_RANGE = (0.55, 1.6)


@dataclass
class LookTransfer:
    strength: float = 0.75
    curve: list[float] = field(default_factory=lambda: list(np.linspace(0, 1, CURVE_POINTS)))
    zone_offsets: list[list[float]] = field(default_factory=lambda: [[0.0] * 3 for _ in range(3)])  # shadows, mids, highlights
    saturation: float = 1.0

    def to_dict(self) -> dict:
        return {
            "strength": round(self.strength, 3),
            "curve": [round(float(v), 5) for v in self.curve],
            "zone_offsets": [[round(float(v), 5) for v in z] for z in self.zone_offsets],
            "saturation": round(float(self.saturation), 4),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "LookTransfer | None":
        if not d:
            return None
        return cls(strength=d["strength"], curve=list(d["curve"]), zone_offsets=[list(z) for z in d["zone_offsets"]],
                   saturation=d["saturation"])


def zone_weights(y: np.ndarray) -> np.ndarray:
    """Smooth shadow / midtone / highlight weights that sum to 1 (shape [..., 3])."""
    y = np.clip(y, 0.0, 1.0)
    sh = np.clip(1.0 - y / 0.45, 0.0, 1.0) ** 2
    hi = np.clip((y - 0.55) / 0.45, 0.0, 1.0) ** 2
    mid = np.clip(1.0 - sh - hi, 0.0, 1.0)
    w = np.stack([sh, mid, hi], axis=-1)
    return w / np.maximum(w.sum(axis=-1, keepdims=True), 1e-6)


def _pool(signals: list[np.ndarray], width: int = 192) -> np.ndarray:
    """Downsample and pool pixels from several frames into one (N, 3) array."""
    import cv2

    out = []
    for s in signals:
        h = max(2, int(round(s.shape[0] * width / s.shape[1])))
        out.append(cv2.resize(s, (width, h), interpolation=cv2.INTER_AREA).reshape(-1, 3))
    return np.clip(np.concatenate(out, axis=0), 0.0, 1.0)


def _chroma_vec(px: np.ndarray) -> np.ndarray:
    return px - colour.luma(px)[:, None]


def _zone_means(px: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Weighted mean chroma vector per zone, and each zone's total weight share."""
    w = zone_weights(colour.luma(px))
    c = _chroma_vec(px)
    tot = w.sum(axis=0)
    means = (w.T @ c) / np.maximum(tot[:, None], 1e-6)
    return means, tot / max(len(px), 1)


def _median_chroma(px: np.ndarray) -> float:
    y = colour.luma(px)
    m = (y > 0.06) & (y < 0.94)
    c = np.linalg.norm(_chroma_vec(px[m] if m.any() else px), axis=1)
    return float(np.median(c))


def _apply_curve(y: np.ndarray, curve: np.ndarray) -> np.ndarray:
    xs = np.linspace(0.0, 1.0, len(curve))
    return np.interp(np.clip(y, 0.0, 1.0), xs, curve).astype(np.float32)


def _tone_curve(shot_y: np.ndarray, ref_y: np.ndarray, strength: float) -> np.ndarray:
    xq = np.percentile(shot_y, QUANTILES)
    yq = np.percentile(ref_y, QUANTILES)
    xq = np.maximum.accumulate(xq + np.arange(len(xq)) * 1e-5)  # strictly increasing
    # Extend to the ends of the range so the curve is defined everywhere.
    xs = np.concatenate([[0.0], xq, [1.0]])
    ys = np.concatenate([[max(0.0, yq[0] - xq[0])], yq, [min(1.0, max(yq[-1], 1.0 - (1.0 - xq[-1])))]])
    ys = np.maximum.accumulate(ys)
    grid = np.linspace(0.0, 1.0, 257)
    f = np.interp(grid, xs, ys)
    # Limit the slope (no posterisation, no flattening), then re-integrate.
    d = np.clip(np.diff(f) / np.diff(grid), *SLOPE_RANGE)
    k = np.array([1, 4, 6, 4, 1], np.float64) / 16
    d = np.convolve(np.pad(d, 2, mode="edge"), k, mode="valid")
    f = np.concatenate([[f[0]], f[0] + np.cumsum(d * np.diff(grid))])
    # Keep the mapped range sensible.
    f = np.clip(f, 0.0, 1.0)
    f = grid + strength * (f - grid)
    return np.interp(np.linspace(0, 1, CURVE_POINTS), grid, f)


def derive_transfer(shot_signals: list[np.ndarray], ref_signals: list[np.ndarray], strength: float = 0.75) -> LookTransfer:
    """Compute a fixed look transfer for one shot from all its samples."""
    strength = float(np.clip(strength, 0.0, 1.0))
    shot = _pool(shot_signals)
    ref = _pool(ref_signals)

    curve = _tone_curve(colour.luma(shot), colour.luma(ref), strength)
    lt = LookTransfer(strength=strength, curve=list(curve))

    # Zone colours are compared *after* the tone curve, because the curve moves
    # pixels between zones.
    toned = apply_transfer(shot, LookTransfer(strength=strength, curve=list(curve)))
    s_means, s_share = _zone_means(toned)
    r_means, r_share = _zone_means(ref)
    offsets = []
    for z in range(3):
        if s_share[z] < 0.01 or r_share[z] < 0.01:
            offsets.append([0.0, 0.0, 0.0])  # a zone one of them barely has: don't guess
            continue
        d = r_means[z] - s_means[z]
        d = d - float(colour.luma(d[None, :])[0])  # keep luma unchanged
        d = np.clip(d, -MAX_ZONE_OFFSET, MAX_ZONE_OFFSET) * strength
        offsets.append([float(v) for v in d])
    lt.zone_offsets = offsets

    toned = apply_transfer(shot, lt, with_saturation=False)
    sat = _median_chroma(ref) / max(_median_chroma(toned), 1e-3)
    lt.saturation = float(np.exp(strength * np.log(np.clip(sat, *SAT_RANGE))))
    return lt


def apply_transfer(sig: np.ndarray, lt: LookTransfer | None, with_saturation: bool = True) -> np.ndarray:
    """Apply a look transfer to a Rec.709 signal array (any leading shape, last dim RGB)."""
    if lt is None:
        return sig
    x = np.asarray(sig, dtype=np.float32)
    y = colour.luma(x)
    y2 = _apply_curve(y, np.asarray(lt.curve, np.float32))
    x = x + (y2 - y)[..., None]
    w = zone_weights(y2)
    x = x + w @ np.asarray(lt.zone_offsets, np.float32)
    if with_saturation and abs(lt.saturation - 1.0) > 1e-4:
        yy = colour.luma(x)[..., None]
        x = yy + (x - yy) * lt.saturation
    return np.clip(x, 0.0, 1.0).astype(np.float32)


def look_distance(a_signals: list[np.ndarray], b_signals: list[np.ndarray]) -> dict:
    """How different two sets of frames look: luma quantiles + zone colours + saturation."""
    a, b = _pool(a_signals), _pool(b_signals)
    qa = np.percentile(colour.luma(a), QUANTILES)
    qb = np.percentile(colour.luma(b), QUANTILES)
    ma, _ = _zone_means(a)
    mb, _ = _zone_means(b)
    return {
        "tone": round(float(np.mean(np.abs(qa - qb))), 4),
        "zones": round(float(np.mean(np.linalg.norm(ma - mb, axis=1))), 4),
        "saturation": round(float(abs(np.log(max(_median_chroma(a), 1e-3) / max(_median_chroma(b), 1e-3)))), 4),
    }
