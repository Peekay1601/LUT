import json

import pytest

from autograde import ai, media
from autograde.pipeline import Options, run, shots_from_manifest


def test_untagged_media_is_rejected_unless_assumed(footage):
    info = media.probe(footage["untagged"])
    assert not info.interpretation.supported
    info = media.probe(footage["untagged"], assume_rec709=True)
    assert info.interpretation.supported and info.interpretation.assumed


def test_matching_reduces_differences(analysed):
    project, out = analysed
    c = project["consistency"]
    assert c["mean_to_reference_after"] < 0.5 * c["mean_to_reference_before"]
    shots = {s["name"]: s for s in project["shots"]}
    dark = shots["b_dark_green.mp4"]
    assert dark["grade"]["exposure_ev"] > 0.3  # was rendered 0.7 EV under
    assert dark["after_vs_reference"]["distance"] < 0.5 * dark["vs_reference"]["distance"]
    assert shots["d_ramp.mp4"]["grade"]["needs_review"]  # exposure ramp can't be fixed by a static grade
    assert shots["a_reference.mp4"]["grade"]["exposure_ev"] == 0


def test_outputs_written(analysed):
    project, out = analysed
    for name in ("project.json", "report.html", "contact_sheet_before.jpg", "contact_sheet_after.jpg", "look/look.cube"):
        assert (out / name).exists(), name
    for s in project["shots"]:
        for key in ("grade_cube", "match_cube", "grade_json", "strip"):
            assert (out / s["files"][key]).exists()
        assert len(s["samples"]) >= 3
        assert s["lut_check"]["max_code_values"] < 2.0
    json.loads((out / "project.json").read_text())


def test_manifest_flags_and_source_ranges(footage, tmp_path):
    manifest = {"clips": [
        {"name": "ok", "source_path": str(footage["a_reference"]), "source_in": 0.5, "source_out": 2.0},
        {"name": "fast", "source_path": str(footage["b_dark_green"]), "source_in": 0, "source_out": 1, "flags": ["speed_change"]},
        {"name": "gone", "source_path": str(tmp_path / "missing.mov"), "source_in": 0, "source_out": 1},
    ]}
    project = run(shots_from_manifest(manifest, tmp_path), tmp_path / "out", Options(), log=lambda m: None)
    status = {s["name"]: s for s in project["shots"]}
    assert status["ok"]["status"] == "ok"
    assert all(0.5 <= f["time"] <= 2.0 for f in status["ok"]["samples"])
    assert status["fast"]["status"] == "skipped" and status["gone"]["status"] == "skipped"


def test_ai_prepare_and_file_roundtrip(footage, tmp_path):
    from autograde.pipeline import shots_from_folder

    opts = Options(reference="a_reference.mp4", ai_mode="prepare")
    shots = shots_from_folder(footage["folder"], opts)[:2]
    run(shots, tmp_path, opts, log=lambda m: None)
    assert (tmp_path / "ai" / "request.md").exists()
    guidance = {"look": {"preset": "natural_commercial", "rationale": "test"}, "shots": [{
        "shot_id": "shot_002",
        "observations": {"exposure": "", "colour_cast": "", "lighting_intent": "", "skin": ""},
        "recommendations": {"exposure_direction": "preserve", "neutral_balance_direction": "auto",
                            "contrast_direction": "auto", "saturation_direction": "auto"},
        "confidence": 0.9, "needs_review": False}]}
    (tmp_path / "ai" / "guidance.json").write_text(json.dumps(guidance))
    opts = Options(reference="a_reference.mp4", ai_mode="file", look="ai",
                   ai_guidance=str(tmp_path / "ai" / "guidance.json"))
    project = run(shots, tmp_path / "second", opts, log=lambda m: None)
    assert project["look"]["name"] == "natural_commercial"
    s2 = next(s for s in project["shots"] if s["shot_id"] == "shot_002")
    assert any("preserve" in r for r in s2["grade"]["reasons"])


def test_ai_validation_rejects_bad_enums():
    with pytest.raises(ValueError):
        ai.validate({"shots": [{"shot_id": "x", "recommendations": {"exposure_direction": "way_brighter"}}]})
