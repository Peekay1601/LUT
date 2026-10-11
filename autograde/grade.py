"""Grade recipes: definition, application, and derivation from measurements.

Transform order (one fixed base grade per shot, never per frame):

    Rec.709 signal in
      -> [linear]  shot exposure (2**ev) and white-balance gains
      -> [signal]  highlight shoulder (soft roll-off so exposure lifts don't hard clip)
      -> [signal]  shot contrast (two-sided power curve around a pivot)
      -> [signal]  shot saturation (around Rec.709 luma)
      -> [linear]  look warmth/tint gains
      -> [signal]  look contrast, saturation, split toning, black lift
      -> clip to [0, 1]
    Rec.709 signal out

Every parameter has a hard bound in ``BOUNDS``. Claude's guidance can only
scale or veto a measured correction inside those bounds; it never writes
parameters directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from . import colour

BOUNDS = {
    "exposure_ev": (-1.0, 1.0),
    "gain": (0.90, 1.10),  # per channel, after luminance normalisation
    "contrast": (0.85, 1.15),
    "saturation": (0.80, 1.20),
}

SHOULDER_KNEE = 0.88


@dataclass
class ShotGrade:
    shot_id: str
    input_space: str = "rec709"
    output_space: str = "rec709"
    exposure_ev: float = 0.0
    rgb_balance: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    contrast: float = 1.0
    contrast_pivot: float = round(colour.MID_GREY_SIGNAL, 4)
    saturation: float = 1.0
    confidence: float = 1.0
    needs_review: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ShotGrade":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Look:
    name: str = "none"
    warmth: float = 0.0  # +warmer / -cooler, roughly "percent" of a gentle WB shift
    tint: float = 0.0  # +magenta / -green
    contrast: float = 1.0
    saturation: float = 1.0
    shadow_tint: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    highlight_tint: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    black_lift: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Look":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


LOOKS: dict[str, Look] = {
    "none": Look(),
    "natural_commercial": Look("natural_commercial", warmth=0.01, contrast=1.04, saturation=1.04),
    "warm_documentary": Look("warm_documentary", warmth=0.04, contrast=1.02, saturation=0.96,
                             shadow_tint=[0.004, 0.0, -0.004], black_lift=0.01),
    "muted_cinematic": Look("muted_cinematic", warmth=0.0, contrast=1.08, saturation=0.84,
                            shadow_tint=[-0.008, 0.002, 0.010], highlight_tint=[0.010, 0.004, -0.008],
                            black_lift=0.02),
    "clean_product": Look("clean_product", warmth=-0.01, contrast=1.06, saturation=1.02),
}


def _clip(v: float, key: str) -> float:
    lo, hi = BOUNDS[key]
    return float(min(hi, max(lo, v)))


def _warmth_gains(warmth: float, tint: float) -> np.ndarray:
    g = np.array([1.0 + warmth, 1.0 - tint, 1.0 - warmth], dtype=np.float64)
    return colour.normalise_gains(g)


def _shoulder(x: np.ndarray, max_in: float, knee: float = SHOULDER_KNEE) -> np.ndarray:
    """Roll highlights off so that signal ``max_in`` (the brightest value a gain
    can produce) lands exactly on 1.0 instead of hard clipping.

    Identity when nothing can exceed 1.0, so pure darkening and identity grades
    are untouched. Slope-continuous at the knee.
    """
    if max_in <= 1.0 + 1e-6:
        return x
    span = 1.0 - knee
    u = np.maximum(x - knee, 0.0) / span
    m = (max_in - knee) / span
    if m <= 2.0:
        c = (m - 1.0) / (m * m)  # h(u) = u - c u^2: h(0)=0, h'(0)=1, h(m)=1, monotone for m<=2
        h = u - c * u * u
    else:
        h = np.tanh(u)
    return np.where(x > knee, knee + span * np.minimum(h, 1.0), x).astype(np.float32)


def _contrast(x: np.ndarray, c: float, pivot: float) -> np.ndarray:
    """Two-sided power curve: 0->0, pivot->pivot, 1->1, slope ``c`` at the pivot."""
    if abs(c - 1.0) < 1e-6:
        return x
    x = np.clip(x, 0.0, 1.0)
    lo = pivot * np.power(x / pivot, c)
    hi = 1.0 - (1.0 - pivot) * np.power(np.clip((1.0 - x) / (1.0 - pivot), 0, None), c)
    return np.where(x < pivot, lo, hi).astype(np.float32)


def _saturation(x: np.ndarray, s: float) -> np.ndarray:
    if abs(s - 1.0) < 1e-6:
        return x
    y = colour.luma(x)[..., None]
    return (y + (x - y) * s).astype(np.float32)


def apply_grade(sig: np.ndarray, grade: ShotGrade | None, look: Look | None = None) -> np.ndarray:
    """Apply a shot grade and an optional shared look to a Rec.709 signal array."""
    x = np.asarray(sig, dtype=np.float32)
    if grade is not None:
        gains = (2.0 ** grade.exposure_ev) * np.asarray(grade.rgb_balance, dtype=np.float32)
        x = colour.oetf_709(colour.inverse_oetf_709(x) * gains)
        x = _shoulder(x, float(colour.oetf_709(np.array(gains.max()))))
        x = _contrast(x, grade.contrast, grade.contrast_pivot)
        x = _saturation(x, grade.saturation)
    if look is not None and look.name != "none":
        if look.warmth or look.tint:
            wg = _warmth_gains(look.warmth, look.tint).astype(np.float32)
            x = colour.oetf_709(colour.inverse_oetf_709(np.clip(x, 0, None)) * wg)
            x = _shoulder(x, float(colour.oetf_709(np.array(wg.max()))))
        x = _contrast(x, look.contrast, colour.MID_GREY_SIGNAL)
        x = _saturation(x, look.saturation)
        y = np.clip(colour.luma(np.clip(x, 0, 1)), 0, 1)[..., None]
        x = x + np.asarray(look.shadow_tint, np.float32) * (1.0 - y) ** 2 + np.asarray(look.highlight_tint, np.float32) * y ** 2
        if look.black_lift:
            x = look.black_lift + (1.0 - look.black_lift) * x
    return np.clip(x, 0.0, 1.0).astype(np.float32)


# --------------------------------------------------------------------------
# Deriving a grade from measurements
# --------------------------------------------------------------------------

# How much of each measured difference we correct. Conservative on purpose:
# matching is meant to remove inconsistency, not erase intentional lighting.
DAMPING = {"exposure": 0.8, "balance": 0.85, "contrast": 0.5, "saturation": 0.5}

DIRECTION_SIGN = {
    "decrease": -1, "decrease_slightly": -1, "preserve": 0,
    "increase_slightly": 1, "increase": 1,
}


@dataclass
class Guidance:
    """Claude's (or a human's) constrained guidance for one shot."""

    exposure_direction: str = "auto"
    neutral_balance_direction: str = "auto"
    contrast_direction: str = "auto"
    saturation_direction: str = "auto"
    lighting_intent: str = ""
    confidence: float = 1.0
    needs_review: bool = False

    @classmethod
    def from_ai(cls, rec: dict) -> "Guidance":
        r = rec.get("recommendations", {})
        obs = rec.get("observations", {})
        return cls(
            exposure_direction=r.get("exposure_direction", "auto"),
            neutral_balance_direction=r.get("neutral_balance_direction", "auto"),
            contrast_direction=r.get("contrast_direction", "auto"),
            saturation_direction=r.get("saturation_direction", "auto"),
            lighting_intent=obs.get("lighting_intent", ""),
            confidence=float(rec.get("confidence", 1.0)),
            needs_review=bool(rec.get("needs_review", False)),
        )


def _steer(measured: float, direction: str, reasons: list[str], label: str) -> tuple[float, bool]:
    """Combine a measured signed correction with a direction hint.

    Returns (strength multiplier, disagreement flag). "auto" trusts the
    measurement; "preserve" mostly cancels it; a contradicting direction
    cancels it and flags the shot for review.
    """
    if direction in ("auto", "", None):
        return 1.0, False
    if direction == "preserve":
        if abs(measured) > 1e-3:
            reasons.append(f"{label}: guidance says preserve; measured correction reduced to 25%")
        return 0.25, False
    sign = DIRECTION_SIGN.get(direction)
    if sign is None:
        return 1.0, False
    if abs(measured) < 1e-3 or np.sign(measured) == sign:
        return (1.0 if "slightly" not in direction else 0.8), False
    reasons.append(f"{label}: guidance ({direction}) contradicts the measurement; not corrected")
    return 0.0, True


# Desired signs of (log R gain, log B gain) for each balance hint (G is the anchor).
_BALANCE_HINTS = {
    "reduce_green_slightly": (1, 1),
    "reduce_magenta_slightly": (-1, -1),
    "warm_slightly": (1, -1),
    "cool_slightly": (-1, 1),
}


def derive_grade(
    shot_id: str,
    shot: dict,
    ref: dict,
    guidance: Guidance | None = None,
    neutral_target: str = "reference",
    flags: list[str] | None = None,
    remeasure=None,
) -> ShotGrade:
    """Turn aggregated shot/reference measurements (plus optional guidance) into a bounded grade."""
    g = guidance or Guidance()
    reasons: list[str] = list(flags or [])
    review = bool(g.needs_review)
    confidence = 1.0

    # ---- exposure -------------------------------------------------------
    # Prefer skin luminance when both shots have believable skin: a close-up
    # and a wide shot should have similar faces, not similar histograms.
    ev_key = float(np.log2(ref["log_key_linear"] / max(shot["log_key_linear"], 1e-6)))
    use_skin = shot["skin"]["confidence"] >= 0.4 and ref["skin"]["confidence"] >= 0.4
    if use_skin:
        ev_skin = float(np.log2(ref["skin"]["median_luminance_linear"] / max(shot["skin"]["median_luminance_linear"], 1e-6)))
        if abs(ev_skin - ev_key) > 0.3 or (np.sign(ev_skin) != np.sign(ev_key) and min(abs(ev_skin), abs(ev_key)) > 0.1):
            # Skin and scene disagree (contrast differences, or the skin region is
            # lit differently). Split the difference and lower confidence.
            ev_meas = 0.5 * (ev_skin + ev_key)
            confidence *= 0.7
            reasons.append(f"exposure: skin ({ev_skin:+.2f} EV) and scene key ({ev_key:+.2f} EV) disagree; used the average")
        else:
            ev_meas = ev_skin
            reasons.append("exposure matched on skin-tone luminance")
    else:
        ev_meas = ev_key
        reasons.append("exposure matched on scene key (no reliable skin in both shots)")
        confidence *= 0.85
    strength, disagree = _steer(ev_meas, g.exposure_direction, reasons, "exposure")
    review |= disagree
    ev = ev_meas * DAMPING["exposure"] * strength
    # Don't push a shot with lots of clipped highlights brighter.
    if ev > 0 and shot["clip_white"] > 0.03:
        ev *= 0.5
        reasons.append("exposure lift halved: shot already has clipped highlights")
    if abs(ev) > BOUNDS["exposure_ev"][1]:
        reasons.append(f"exposure correction {ev:+.2f} EV clamped")
        review = True
    ev = _clip(ev, "exposure_ev")

    if shot["exposure_variation_ev"] > 0.35:
        reasons.append(
            f"exposure varies {shot['exposure_variation_ev']:.2f} EV within the shot; a fixed grade cannot follow it"
        )
        review = True

    # ---- white balance --------------------------------------------------
    n_conf = min(shot["neutral"]["confidence"], 1.0)
    if neutral_target == "reference":
        target_rg, target_bg = ref["neutral"]["r_over_g"], ref["neutral"]["b_over_g"]
        n_conf = min(n_conf, ref["neutral"]["confidence"])
    else:
        target_rg, target_bg = 1.0, 1.0
    gain_r = target_rg / max(shot["neutral"]["r_over_g"], 1e-4)
    gain_b = target_bg / max(shot["neutral"]["b_over_g"], 1e-4)
    log_r, log_b = float(np.log(gain_r)), float(np.log(gain_b))

    hint = g.neutral_balance_direction
    bal_strength = 1.0
    if hint == "preserve":
        bal_strength = 0.25
        reasons.append("balance: guidance says lighting colour is intentional; correction reduced to 25%")
    elif hint in _BALANCE_HINTS:
        want = _BALANCE_HINTS[hint]
        agree = all(abs(v) < 0.005 or np.sign(v) == w_ for v, w_ in zip((log_r, log_b), want))
        if not agree:
            bal_strength = 0.0
            review = True
            reasons.append(f"balance: guidance ({hint}) contradicts neutral measurement; not corrected")
    if g.lighting_intent and any(w in g.lighting_intent.lower() for w in ("preserve", "intentional", "practical", "motivated")):
        bal_strength = min(bal_strength, 0.5)

    if n_conf < 0.2:
        reasons.append("balance: too few reliable neutral pixels; balance left unchanged")
        bal_strength = 0.0
        if max(abs(log_r), abs(log_b)) > 0.08:
            review = True
            reasons.append("balance: possible colour shift that could not be verified on neutrals")
    w = DAMPING["balance"] * bal_strength * min(1.0, n_conf / 0.6)
    gains = np.exp(np.array([log_r * w, 0.0, log_b * w]))
    gains = colour.normalise_gains(gains)
    lo, hi = BOUNDS["gain"]
    if np.any(gains < lo) or np.any(gains > hi):
        reasons.append("balance: gains clamped to ±10%")
        review = True
    gains = colour.normalise_gains(gains, lo, hi)
    confidence *= 0.6 + 0.4 * min(1.0, n_conf / 0.6)

    # ---- contrast / saturation (display-referred, after exposure) ------
    # Exposure changes signal-space spread and chroma, so when the caller can
    # re-measure the samples with exposure+balance applied, compare those.
    post = shot
    if remeasure is not None:
        post = remeasure(ShotGrade(shot_id=shot_id, exposure_ev=ev, rgb_balance=[float(v) for v in gains]))
    c_meas = ref["contrast_spread"] / max(post["contrast_spread"], 1e-3)
    strength, disagree = _steer(np.log(c_meas), g.contrast_direction, reasons, "contrast")
    review |= disagree
    contrast = _clip(float(np.exp(np.log(c_meas) * DAMPING["contrast"] * strength)), "contrast")

    s_meas = ref["chroma_p50"] / max(post["chroma_p50"], 1e-3)
    strength, disagree = _steer(np.log(s_meas), g.saturation_direction, reasons, "saturation")
    review |= disagree
    saturation = _clip(float(np.exp(np.log(s_meas) * DAMPING["saturation"] * strength)), "saturation")

    confidence = round(float(confidence * (g.confidence if guidance else 1.0)), 3)
    if confidence < 0.5:
        review = True
        reasons.append(f"overall confidence {confidence:.2f} below 0.5")

    return ShotGrade(
        shot_id=shot_id,
        exposure_ev=round(ev, 3),
        rgb_balance=[round(float(v), 4) for v in gains],
        contrast=round(contrast, 4),
        saturation=round(saturation, 4),
        confidence=confidence,
        needs_review=bool(review),
        reasons=reasons,
    )
