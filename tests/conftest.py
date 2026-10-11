import pytest

from tests.fixtures import make_set, render_clip


@pytest.fixture(scope="session")
def footage(tmp_path_factory):
    folder = tmp_path_factory.mktemp("footage")
    clips = make_set(folder)
    untagged = tmp_path_factory.mktemp("untagged")
    clips["untagged"] = render_clip(untagged / "untagged.mp4", seconds=1.0, tag=False)
    clips["folder"] = folder
    return clips


@pytest.fixture(scope="session")
def analysed(footage, tmp_path_factory):
    from autograde.pipeline import Options, run, shots_from_folder

    out = tmp_path_factory.mktemp("results")
    opts = Options(reference="a_reference.mp4", look="muted_cinematic")
    return run(shots_from_folder(footage["folder"], opts), out, opts, log=lambda m: None), out
