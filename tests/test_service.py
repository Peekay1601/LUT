import time

from fastapi.testclient import TestClient

from autograde.service import create_app

TOKEN = "test-token"


def _client(tmp_path):
    return TestClient(create_app(tmp_path, token=TOKEN), headers={"X-CG-Token": TOKEN})


def test_service_job(footage, tmp_path):
    client = _client(tmp_path)
    assert client.get("/health").json()["ffmpeg"]
    manifest = {"clips": [
        {"name": "a", "source_path": str(footage["a_reference"]), "source_in": 0, "source_out": 2},
        {"name": "b", "source_path": str(footage["b_dark_green"]), "source_in": 0, "source_out": 2},
    ]}
    assert client.post("/analyze", json={"manifest": manifest, "options": {"bogus": 1}}).status_code == 400
    bad = {"clips": [{"name": "x", "source_path": "relative.mp4", "source_in": 0, "source_out": 1}]}
    assert client.post("/analyze", json={"manifest": bad}).status_code == 400
    bad = {"clips": [{"name": "x", "source_path": "/etc/passwd", "source_in": 0, "source_out": 1}]}
    assert client.post("/analyze", json={"manifest": bad}).status_code == 400
    job_id = client.post("/analyze", json={"manifest": manifest, "options": {"reference": "shot_001"}}).json()["job_id"]
    for _ in range(120):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.5)
    assert job["status"] == "done", job.get("error")
    assert client.get(job["summary"]["report"]).status_code == 200
    assert job["progress"] == 1.0
    # Results and jobs are not readable without the token.
    anon = TestClient(create_app(tmp_path, token=TOKEN))
    assert anon.get(job["summary"]["report"]).status_code == 401
    assert anon.get(f"/jobs/{job_id}").status_code == 401
    assert anon.get(job["summary"]["report"] + "?token=" + TOKEN).status_code == 200
    # Approvals are persisted next to the job.
    assert client.post(f"/jobs/{job_id}/approvals", json={"shot_002": True}).json() == {"shot_002": True}
    assert client.post(f"/jobs/{job_id}/approvals", json={"shot_999": True}).status_code == 400
    assert job["summary"]["shots"][1]["grade"]["exposure_ev"] > 0.3


def _wait(client, job_id):
    for _ in range(120):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.5)
    raise AssertionError("job did not finish")


def test_api_key_is_used_but_never_stored(footage, tmp_path, monkeypatch):
    from autograde import ai

    seen = {}

    def fake_request(ref, sheets, text, model=ai.MODEL, api_key=None):
        seen["key"] = api_key
        return {"look": {"preset": "clean_product", "rationale": "test"}, "shots": []}

    monkeypatch.setattr(ai, "request_guidance", fake_request)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path)
    manifest = {"clips": [{"name": "a", "source_path": str(footage["a_reference"]), "source_in": 0, "source_out": 1.5}]}

    # Claude requested without any key -> clear error.
    job = _wait(client, client.post("/analyze", json={"manifest": manifest, "options": {"ai_mode": "api"}}).json()["job_id"])
    assert job["status"] == "error" and "API key" in job["error"]

    secret = "sk-ant-test-0123456789abcdef"
    job = _wait(client, client.post("/analyze", json={
        "manifest": manifest, "options": {"ai_mode": "api", "look": "ai"}, "api_key": secret}).json()["job_id"])
    assert job["status"] == "done", job.get("error")
    assert seen["key"] == secret
    assert job["summary"]["look"] == "clean_product"
    for f in (tmp_path / job["job_id"]).rglob("*"):
        if f.is_file() and f.suffix in (".json", ".html", ".md", ".cube"):
            assert secret not in f.read_text(), f
    assert secret not in str(job)


def test_check_key_rejects_bad_key(tmp_path):
    client = _client(tmp_path)
    res = client.post("/ai/check-key", json={"api_key": "sk-ant-not-a-real-key-000000"}).json()
    assert res["ok"] is False and "sk-ant-not" not in res["error"]


def test_cancel(footage, tmp_path):
    client = _client(tmp_path)
    manifest = {"clips": [{"name": n, "source_path": str(footage[n]), "source_in": 0, "source_out": 3}
                          for n in ("a_reference", "b_dark_green", "c_warm_flat", "d_ramp")]}
    job_id = client.post("/analyze", json={"manifest": manifest}).json()["job_id"]
    client.post(f"/jobs/{job_id}/cancel")
    job = _wait(client, job_id)
    assert job["status"] == "cancelled"
