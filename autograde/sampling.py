"""Representative frame selection.

Never just the first frame: we decode a pool of candidate times spread through
the shot (skipping the head and tail where fades, transitions and camera
settling live), throw away black/flat frames and frames that look like a
transition, then keep an evenly spread subset.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import colour, measure
from .media import MediaInfo, extract_frame


@dataclass
class Sample:
    time: float  # seconds in the *source file*
    signal: np.ndarray
    quality: dict


def target_count(duration: float, base: int = 5, per_seconds: float = 6.0, cap: int = 12) -> int:
    """~5 samples for short shots, one extra per ``per_seconds`` after 10 s, capped."""
    extra = max(0, int((duration - 10.0) // per_seconds)) if duration > 10.0 else 0
    return int(min(cap, base + extra))


def _luma_hist(sig: np.ndarray) -> np.ndarray:
    h, _ = np.histogram(colour.luma(sig), bins=32, range=(0, 1))
    return h / max(h.sum(), 1)


def sample_shot(
    info: MediaInfo,
    start: float,
    end: float,
    count: int | None = None,
    width: int = 960,
    cap: int = 12,
    cache_dir: Path | None = None,
) -> tuple[list[Sample], list[str]]:
    """Return evenly spread usable samples in [start, end) and any warnings."""
    warnings: list[str] = []
    duration = max(end - start, 0.0)
    n = count or target_count(duration, cap=cap)
    frame = 1.0 / info.fps if info.fps else 0.04
    if duration < 3 * frame:
        warnings.append("shot is shorter than three frames; sampled once")
        n = 1
    # Skip 8% at each end (at least 2 frames) to avoid transitions and fades.
    margin = max(0.08 * duration, 2 * frame) if duration > 6 * frame else 0.0
    a, b = start + margin, end - margin - frame
    if b <= a:
        a, b = start, max(start, end - frame)
    pool_n = max(n * 2, n + 2) if n > 1 else 1
    times = np.linspace(a, b, pool_n) if pool_n > 1 else np.array([(a + b) / 2])

    pool: list[Sample] = []
    for t in times:
        sig = extract_frame(info, float(t), width=width, cache_dir=cache_dir)
        pool.append(Sample(float(t), sig, measure.frame_quality(sig)))

    usable = [s for s in pool if not s.quality["is_black"] and not s.quality["is_flat"]]
    dropped = len(pool) - len(usable)
    if dropped:
        warnings.append(f"dropped {dropped} black/flat candidate frame(s)")

    # Transition heuristic: a frame whose histogram differs a lot from *both*
    # neighbours while the neighbours agree with each other is probably a flash,
    # dissolve frame or cut-in-progress.
    if len(usable) >= 3:
        hists = [_luma_hist(s.signal) for s in usable]
        keep = [True] * len(usable)
        for i in range(1, len(usable) - 1):
            d_prev = 0.5 * np.abs(hists[i] - hists[i - 1]).sum()
            d_next = 0.5 * np.abs(hists[i] - hists[i + 1]).sum()
            d_nb = 0.5 * np.abs(hists[i - 1] - hists[i + 1]).sum()
            if d_prev > 0.35 and d_next > 0.35 and d_nb < 0.2:
                keep[i] = False
        if not all(keep):
            warnings.append(f"dropped {keep.count(False)} probable transition frame(s)")
        usable = [s for s, k in zip(usable, keep) if k]

    if not usable:
        warnings.append("no usable frames found (all black or flat); shot needs review")
        return [], warnings

    if len(usable) > n:
        idx = np.round(np.linspace(0, len(usable) - 1, n)).astype(int)
        # Prefer the sharper of the two neighbours at each pick to avoid motion blur.
        chosen = []
        for i in idx:
            cands = [j for j in (i - 1, i, i + 1) if 0 <= j < len(usable) and j not in chosen]
            chosen.append(max(cands, key=lambda j: usable[j].quality["sharpness"]) if cands else i)
        usable = [usable[j] for j in sorted(set(chosen))]
    return usable, warnings
