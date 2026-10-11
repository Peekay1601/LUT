from autograde.grade import BOUNDS, Guidance, derive_grade


def _meas(key=0.1, rg=1.0, bg=1.0, spread=0.5, chroma=0.2, skin=0.0, var=0.0):
    return {
        "log_key_linear": key, "contrast_spread": spread, "chroma_p50": chroma, "clip_white": 0.0,
        "exposure_variation_ev": var,
        "neutral": {"r_over_g": rg, "b_over_g": bg, "confidence": 0.9},
        "skin": {"confidence": skin},
    }


def test_same_shot_needs_no_correction():
    g = derive_grade("s", _meas(), _meas())
    assert g.exposure_ev == 0 and g.rgb_balance == [1.0, 1.0, 1.0] and g.contrast == 1 and g.saturation == 1
    assert not g.needs_review


def test_direction_and_bounds():
    g = derive_grade("s", _meas(key=0.01, rg=0.7), _meas(key=0.1))
    lo, hi = BOUNDS["exposure_ev"]
    assert g.exposure_ev == hi  # darker shot is brightened, clamped
    assert g.rgb_balance[0] > g.rgb_balance[1]  # too little red -> red gain up
    assert all(BOUNDS["gain"][0] - 1e-3 <= v <= BOUNDS["gain"][1] + 1e-3 for v in g.rgb_balance)
    assert g.needs_review  # clamped corrections are flagged


def test_guidance_can_preserve_or_veto():
    shot, ref = _meas(rg=1.15, bg=0.85), _meas()
    auto = derive_grade("s", shot, ref)
    keep = derive_grade("s", shot, ref, Guidance(neutral_balance_direction="preserve"))
    assert abs(keep.rgb_balance[0] - 1) < abs(auto.rgb_balance[0] - 1)
    # "warm" contradicts a measured need to cool -> no correction, flagged.
    veto = derive_grade("s", shot, ref, Guidance(neutral_balance_direction="warm_slightly"))
    assert veto.rgb_balance == [1.0, 1.0, 1.0] and veto.needs_review


def test_unstable_exposure_flagged():
    assert derive_grade("s", _meas(var=0.6), _meas()).needs_review


def test_low_neutral_confidence_leaves_balance():
    shot = _meas(rg=1.3)
    shot["neutral"]["confidence"] = 0.05
    g = derive_grade("s", shot, _meas())
    assert g.rgb_balance == [1.0, 1.0, 1.0]
