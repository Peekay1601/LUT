import numpy as np
from PIL import Image

from autograde import colour, transfer
from autograde.grade import ShotGrade, apply_grade
from autograde.media import extract_frame, probe
from autograde.pipeline import Options, run, shots_from_manifest


def stylise(sig: np.ndarray) -> np.ndarray:
    """A dark teal/orange look: crushed blacks, cool shadows, warm highlights, muted colour."""
    y = colour.luma(sig)[..., None]
    out = np.clip((sig - 0.06) / 0.9, 0, 1) ** 1.35            # crush + darken
    yo = colour.luma(out)[..., None]
    out = yo + (out - yo) * 0.7                                 # desaturate
    w_sh = np.clip(1 - y / 0.45, 0, 1) ** 2
    w_hi = np.clip((y - 0.5) / 0.5, 0, 1) ** 2
    out = out + w_sh * np.array([-0.03, 0.01, 0.06]) + w_hi * np.array([0.08, 0.02, -0.07])
    return np.clip(out, 0, 1).astype(np.float32)


def to_srgb_png(sig, path):
    lin = colour.inverse_oetf_709(sig)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    Image.fromarray((np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)).save(path)


def test_identity_when_reference_equals_shot(footage):
    sig = extract_frame(probe(footage["a_reference"]), 1.0, width=320)
    lt = transfer.derive_transfer([sig], [sig], 1.0)
    out = transfer.apply_transfer(sig, lt)
    assert np.abs(out - sig).mean() < 0.01


def test_curve_monotonic_and_bounded(footage):
    a = extract_frame(probe(footage["a_reference"]), 1.0, width=320)
    b = stylise(extract_frame(probe(footage["c_warm_flat"]), 1.0, width=320))
    lt = transfer.derive_transfer([a], [b], 1.0)
    c = np.asarray(lt.curve)
    assert np.all(np.diff(c) >= -1e-6) and c.min() >= 0 and c.max() <= 1
    assert np.all(np.abs(np.asarray(lt.zone_offsets)) <= transfer.MAX_ZONE_OFFSET + 1e-6)
    weird = np.array([[[2.0, -1.0, 0.5], [np.nan, 0.2, 0.2]]], np.float32)
    out = transfer.apply_transfer(np.nan_to_num(weird), lt)
    assert np.isfinite(out).all() and out.min() >= 0 and out.max() <= 1


def test_look_match_moves_shot_towards_stylised_reference(footage, tmp_path):
    # Reference: a *different* clip, heavily stylised, given as a still image.
    ref_sig = stylise(extract_frame(probe(footage["b_dark_green"]), 1.5))
    ref_png = tmp_path / "ref.png"
    to_srgb_png(ref_sig, ref_png)
    manifest = {"clips": [{"name": "warm", "source_path": str(footage["c_warm_flat"]), "source_in": 0, "source_out": 3}]}
    results = {}
    for strength in (0.5, 1.0):
        opts = Options(reference=str(ref_png), look_match=True, look_strength=strength)
        project = run(shots_from_manifest(manifest, tmp_path), tmp_path / f"o{strength}", opts, log=lambda m: None)
        s = project["shots"][0]
        assert s["grade"]["look_transfer"]["strength"] == strength
        results[strength] = s["look_distance"]
    before, half, full = results[1.0]["before"], results[0.5]["after"], results[1.0]["after"]
    assert full["tone"] < 0.4 * before["tone"], (before, full)
    assert full["zones"] < 0.5 * before["zones"], (before, full)
    assert full["tone"] <= half["tone"] + 1e-6 and full["zones"] <= half["zones"] + 1e-6  # strength is monotonic


def test_lut_includes_transfer(footage):
    from autograde import lut
    a = extract_frame(probe(footage["a_reference"]), 1.0, width=320)
    b = stylise(a)
    lt = transfer.derive_transfer([a], [b], 1.0)
    g = ShotGrade("x", look_transfer=lt.to_dict())
    err = lut.lut_error(a, g, None, 33)
    assert err["mean_code_values"] < 0.6 and err["max_code_values"] < 6
    assert np.abs(apply_grade(a, g) - a).mean() > 0.02  # it actually does something
