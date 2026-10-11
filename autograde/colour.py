"""Colour-space definitions used by every other module.

There is exactly one working convention in the MVP, and every image array that
moves between modules follows it:

* **Signal** ("encoded"): Rec.709 R'G'B', full-range normalised to [0, 1],
  float32, shape (H, W, 3). Limited-range (16-235) video is expanded to this
  range at decode time by FFmpeg, using the stream's declared range.
* **Linear**: scene-linear Rec.709 primaries obtained with the inverse of the
  Rec.709 OETF. Exposure (EV) and white-balance gains are multiplications in
  this domain, so they behave like changing light rather than like a curve.

Contrast, saturation and split toning are display-referred operations and run
on the signal. ``grade.apply_grade`` documents the exact order.

LUTs written by ``lut.py`` take signal in and return signal out, i.e. they are
Rec.709 -> Rec.709 "creative/correction" LUTs, not input transforms.
"""

from __future__ import annotations

import numpy as np

# Rec.709 / sRGB primaries share these luma coefficients.
LUMA_709 = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Exact-continuity constants (1.099 / 0.018 rounded in the spec leave a tiny jump).
_ALPHA = 1.09929682680944
_BETA = 0.018053968510807
_GAMMA = 0.45

# Rec.709 OETF of 18% grey; used as the default contrast pivot.
MID_GREY_LINEAR = 0.18


def oetf_709(lin: np.ndarray) -> np.ndarray:
    """Scene-linear -> Rec.709 signal (ITU-R BT.709 OETF)."""
    lin = np.asarray(lin, dtype=np.float32)
    lin = np.maximum(lin, 0.0)
    return np.where(
        lin < _BETA,
        4.5 * lin,
        _ALPHA * np.power(lin, _GAMMA) - (_ALPHA - 1.0),
    ).astype(np.float32)


def inverse_oetf_709(sig: np.ndarray) -> np.ndarray:
    """Rec.709 signal -> scene-linear (exact inverse of ``oetf_709``)."""
    sig = np.asarray(sig, dtype=np.float32)
    sig = np.clip(sig, 0.0, None)
    return np.where(
        sig < 4.5 * _BETA,
        sig / 4.5,
        np.power((sig + (_ALPHA - 1.0)) / _ALPHA, 1.0 / _GAMMA),
    ).astype(np.float32)


MID_GREY_SIGNAL = float(oetf_709(np.array(MID_GREY_LINEAR)))


def luma(sig: np.ndarray) -> np.ndarray:
    """Rec.709 luma Y' of a signal image (or linear luminance Y of a linear image)."""
    return np.tensordot(sig, LUMA_709, axes=([-1], [0])).astype(np.float32)


def chroma(sig: np.ndarray) -> np.ndarray:
    """A simple, robust saturation measure: (max - min) / max, 0 for black."""
    mx = sig.max(axis=-1)
    mn = sig.min(axis=-1)
    return np.where(mx > 1e-4, (mx - mn) / np.maximum(mx, 1e-4), 0.0).astype(np.float32)


def normalise_gains(gains: np.ndarray, lo: float | None = None, hi: float | None = None) -> np.ndarray:
    """Scale RGB gains so that they leave Rec.709 luminance of grey unchanged.

    With ``lo``/``hi`` the gains are also kept inside those bounds (alternating
    clip and normalise converges in a few steps for sensible bounds).
    """
    gains = np.asarray(gains, dtype=np.float64)
    gains = gains / float(np.dot(gains, LUMA_709))
    if lo is not None and hi is not None:
        for _ in range(8):
            gains = np.clip(gains, lo, hi)
            gains = gains / float(np.dot(gains, LUMA_709))
        gains = np.clip(gains, lo, hi)
    return gains


# What we accept as "known SDR Rec.709" from ffprobe metadata.
ACCEPTED_PRIMARIES = {"bt709"}
# smpte170m (Rec.601) uses the same OETF as Rec.709.
ACCEPTED_TRANSFERS = {"bt709", "smpte170m"}
ACCEPTED_MATRICES = {"bt709"}
