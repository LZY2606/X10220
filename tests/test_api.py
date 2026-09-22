"""HTTP API 测试：走 FastAPI TestClient，临时数据库，不访问公网。"""
import pathlib

import pytest
from fastapi.testclient import TestClient

from nmrb.app import app
from nmrb.dependencies import reset_state, get_state
from nmrb.scenario import CASE_DIR


@pytest.fixture
def client(tmp_path):
    state = reset_state(str(tmp_path / "api.db"))
    with TestClient(app) as c:
        yield c
    state.conn.close()


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_demo_case_workflow(client):
    out = client.post("/api/demo-case").json()
    pid = out["project_id"]
    snap = client.get(f"/api/projects/{pid}/snapshot").json()
    assert snap["skeleton"]["sites"]
    assert any(r["state"] == "current" for r in snap["results"])
    codes = {f["code"] for f in snap["scenario_findings"]}
    assert {"OVERLAP_PEAK", "TIED_BEST", "EDITOR_CONFLICT"} <= codes


def test_import_bad_format_returns_diagnostics_not_500(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    bad = "@format NMRB-PEAKS1/9\n@p 1 2 3 s\n"
    r = client.post(f"/api/projects/{pid}/sources",
                    json={"logical_name": "bad.pk1", "text": bad,
                          "change_note": "故意格式错误"})
    assert r.status_code == 200
    body = r.json()
    assert any(d["code"] == "UNKNOWN_FORMAT" for d in body["diagnostics"])


def test_decision_creates_branch_and_exports(client):
    pid = client.post("/api/demo-case").json()["project_id"]
    snap = client.get(f"/api/projects/{pid}/snapshot").json()
    h = next(r for r in snap["results"] if r["nucleus"] == "1H" and r["state"] == "current")
    site_id = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_A")
    peak_id = h["candidate_map"][site_id][0]["peak_id"]
    head = snap["head_event"]
    r = client.post(f"/api/projects/{pid}/decisions",
                    json={"site_id": site_id, "peak_id": peak_id, "result_id": h["id"],
                          "verdict": "accept", "actor": "tester", "rationale": "api 测试",
                          "parent_event": head, "branch": "review-test"})
    assert r.status_code == 200
    assert r.json()["branch"] == "review-test"
    bundle = client.get(f"/api/projects/{pid}/export").json()
    assert bundle["bundle_format"] == "NMRB-BUNDLE/1"
    assert bundle["tables"]["events"]


def test_reimport_bundle_via_api(client, tmp_path):
    pid = client.post("/api/demo-case").json()["project_id"]
    bundle = client.get(f"/api/projects/{pid}/export").json()
    r = client.post("/api/import-bundle", json={"bundle": bundle})
    assert r.status_code == 200
    assert r.json()["project_id"] == pid


def test_case_manifest_and_file_served(client):
    m = client.get("/api/case-manifest").json()
    assert "共振经纬" in m["case_title"]
    f = client.get("/api/case-file", params={"name": m["baseline"][1]}).json()
    assert "@format" in f["text"]
