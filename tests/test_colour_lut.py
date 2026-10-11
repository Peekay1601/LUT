import numpy as np

from autograde import colour, lut
from autograde.grade import LOOKS, ShotGrade, apply_grade


def test_oetf_roundtrip():
    x = np.linspace(0, 1, 1001, dtype=np.float32)
    assert np.allclose(colour.oetf_709(colour.inverse_oetf_709(x)), x, atol=1e-5)
    assert abs(colour.MID_GREY_SIGNAL - 0.409) < 1e-3


def test_identity_grade_is_identity():
    rng = np.random.default_rng(0)
    img = rng.random((20, 30, 3), dtype=np.float32)
    assert np.allclose(apply_grade(img, ShotGrade("x"), None), img, atol=1e-5)
    assert np.allclose(apply_grade(img, None, LOOKS["none"]), img, atol=1e-6)


def test_exposure_is_linear_light():
    grey = np.full((1, 1, 3), colour.MID_GREY_SIGNAL, np.float32)
    out = apply_grade(grey, ShotGrade("x", exposure_ev=1.0), None)
    # +1 EV doubles scene-linear light (0.18 -> 0.36), shoulder not reached.
    assert abs(float(colour.inverse_oetf_709(out)[0, 0, 0]) - 0.36) < 2e-3


def test_balance_gains_preserve_grey_luminance():
    g = colour.normalise_gains([1.1, 1.0, 0.9])
    assert abs(float(np.dot(g, colour.LUMA_709)) - 1.0) < 1e-9


def test_cube_order_and_roundtrip(tmp_path):
    grade = ShotGrade("x", exposure_ev=0.3, rgb_balance=[1.03, 0.99, 0.97], contrast=1.05, saturation=0.95)
    table = lut.build_lut(grade, LOOKS["warm_documentary"], 17)
    path = tmp_path / "t.cube"
    lut.write_cube(path, table, 17, "t")
    size, back = lut.read_cube(path)
    assert size == 17 and back.shape == (17 ** 3, 3)
    assert np.allclose(back, table, atol=1e-5)
    # Red varies fastest: second row is (r=1/16, g=0, b=0) input.
    expected = apply_grade(np.array([[[1 / 16, 0, 0]]], np.float32), grade, LOOKS["warm_documentary"])[0, 0]
    assert np.allclose(back[1], expected, atol=1e-5)


def test_lut_matches_direct_transform():
    rng = np.random.default_rng(1)
    img = rng.random((64, 64, 3), dtype=np.float32)
    grade = ShotGrade("x", exposure_ev=0.5, rgb_balance=[1.05, 0.98, 0.99], contrast=1.1, saturation=1.1)
    err = lut.lut_error(img, grade, LOOKS["muted_cinematic"], 33)
    assert err["mean_code_values"] < 0.5 and err["max_code_values"] < 3.0
