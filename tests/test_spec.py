"""Tests named after the requirements in the build brief."""

import json

import numpy as np

from autograde import colour, lut, measure
from autograde.grade import LOOKS, ShotGrade, apply_grade
from autograde.media import extract_frame, probe
from autograde.pipeline import Options, run, shots_from_folder, shots_from_manifest


def _grey_ramp(n=256):
    v = np.linspace(0, 1, n, dtype=np.float32)
    return np.repeat(v[None, :, None], 3, axis=2)


def test_neutral_ramp_stays_neutral():
    # Exposure, contrast, saturation and neutral looks must not tint greys.
    for grade, look in [
        (ShotGrade("x", exposure_ev=0.7, contrast=1.1, saturation=0.9), None),
        (ShotGrade("x", exposure_ev=-0.5), LOOKS["natural_commercial"].__class__(name="n", contrast=1.1, saturation=1.2)),
    ]:
        out = apply_grade(_grey_ramp(), grade, look)
        assert np.max(out.max(axis=-1) - out.min(axis=-1)) < 1e-5
        assert np.all(np.diff(out[0, :, 0]) >= -1e-6)  # monotonic


def test_balance_gains_neutralise_a_cast_ramp():
    cast = np.array([0.93, 1.06, 0.95], np.float32)
    ramp = colour.oetf_709(colour.inverse_oetf_709(_grey_ramp()) * cast)
    fix = colour.normalise_gains(1.0 / cast)
    out = apply_grade(ramp, ShotGrade("x", rgb_balance=list(fix)), None)
    mid = out[0, 40:200]  # away from the black floor and the highlight shoulder
    assert np.max(mid.max(axis=-1) - mid.min(axis=-1)) < 2e-3


def test_out_of_domain_and_clipping():
    weird = np.array([[[-0.5, 0.2, 1.7], [np.inf, 0.0, 0.0], [2.0, 2.0, 2.0]]], np.float32)
    out = apply_grade(np.nan_to_num(weird, posinf=1.0), ShotGrade("x", exposure_ev=1.0, saturation=1.2), LOOKS["muted_cinematic"])
    assert np.all(np.isfinite(out)) and out.min() >= 0 and out.max() <= 1
    # Exposure lifts roll off instead of hard clipping: distinct inputs stay distinct below 1.0.
    hi = apply_grade(np.array([[[0.80] * 3, [0.90] * 3, [0.95] * 3]], np.float32), ShotGrade("x", exposure_ev=0.3), None)
    v = hi[0, :, 0]
    assert v[0] < v[1] < v[2] <= 1.0
    # LUT application clamps out-of-range input to the lattice.
    table = lut.build_lut(ShotGrade("x"), None, 17)
    assert np.allclose(lut.apply_cube(np.array([[[1.5, -0.2, 0.5]]], np.float32), 17, table), [[[1.0, 0.0, 0.5]]], atol=1e-5)


def test_lut_interpolation_exact_on_lattice():
    size = 9
    grade = ShotGrade("x", exposure_ev=0.4, rgb_balance=[1.04, 0.98, 1.0], contrast=1.1)
    table = lut.build_lut(grade, None, size)
    pts = lut.lattice(size).reshape(1, -1, 3)
    assert np.allclose(lut.apply_cube(pts, size, table), table.reshape(1, -1, 3), atol=1e-6)


def test_known_exposure_and_cast_recovered(analysed):
    project, _ = analysed
    shot = next(s for s in project["shots"] if s["name"] == "b_dark_green.mp4")
    # Rendered with r/g = 0.877, b/g = 0.896 relative to the reference on true neutrals.
    before, after = shot["vs_reference"], shot["after_vs_reference"]
    assert abs(before["neutral_r_over_g"] - 0.877) < 0.03 and abs(before["neutral_b_over_g"] - 0.896) < 0.03
    assert abs(np.log(after["neutral_r_over_g"])) < 0.35 * abs(np.log(before["neutral_r_over_g"]))
    assert abs(after["exposure_ev"]) < 0.35 * abs(before["exposure_ev"])


def test_deterministic(footage, tmp_path):
    opts = Options(reference="a_reference.mp4", ai_mode="mock", look="ai")
    shots = shots_from_folder(footage["folder"], opts)[:2]
    a = run(shots, tmp_path / "a", opts, log=lambda m: None)
    b = run(shots, tmp_path / "b", opts, log=lambda m: None)
    assert [s["grade"] for s in a["shots"]] == [s["grade"] for s in b["shots"]]
    assert (tmp_path / "a/shot_002/grade.cube").read_text() == (tmp_path / "b/shot_002/grade.cube").read_text()
    assert a["look"]["name"] == "natural_commercial"  # from the mock provider


def test_source_time_not_timeline_time(footage, tmp_path):
    # Clip sits late on the timeline but uses the first 1.5 s of the source.
    manifest = {"clips": [{"name": "late", "source_path": str(footage["d_ramp"]), "source_in": 0.0, "source_out": 1.5,
                           "timeline_in": 100.0, "timeline_out": 101.5}]}
    project = run(shots_from_manifest(manifest, tmp_path), tmp_path / "o", Options(), log=lambda m: None)
    s = project["shots"][0]
    assert all(0.0 <= f["time"] <= 1.5 for f in s["samples"])
    assert s["timeline"] == {"timeline_in": 100.0, "timeline_out": 101.5}


def test_failure_recovery_corrupt_file(footage, tmp_path):
    folder = tmp_path / "mixed"
    folder.mkdir()
    (folder / "broken.mp4").write_bytes(b"not a video")
    (folder / "good.mp4").write_bytes(footage["a_reference"].read_bytes())
    project = run(shots_from_folder(folder, Options()), tmp_path / "o", Options(), log=lambda m: None)
    status = {s["name"]: s["status"] for s in project["shots"]}
    assert status == {"broken.mp4": "skipped", "good.mp4": "ok"}


def test_temporal_stability_one_grade_per_shot(analysed):
    project, out = analysed
    for s in project["shots"]:
        # A single grade file and LUT per shot, regardless of how many samples.
        assert len(list((out / s["shot_id"]).glob("*.cube"))) == 2
        assert json.loads((out / s["files"]["grade_json"]).read_text()) == s["grade"]


def test_frame_cache_is_lossless(footage, tmp_path):
    info = probe(footage["a_reference"])
    direct = extract_frame(info, 1.0)
    first = extract_frame(info, 1.0, cache_dir=tmp_path)
    again = extract_frame(info, 1.0, cache_dir=tmp_path)
    assert len(list(tmp_path.glob("*.npy"))) == 1
    assert np.array_equal(direct, first) and np.array_equal(first, again)


def test_limited_range_is_expanded(footage):
    # Fixture is limited range; the 80% grey card must decode near its true signal value.
    sig = extract_frame(probe(footage["a_reference"]), 1.0, width=640)
    card = sig[220:280, 260:290]  # 80% card at offset 0: x 250..310
    expected = float(colour.oetf_709(np.array(0.80)))
    assert abs(float(np.median(colour.luma(card))) - expected) < 0.02
    assert measure.frame_quality(sig)["is_black"] is False
