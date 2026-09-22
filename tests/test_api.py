"""端到端 HTTP：导入(含格式错误) -> 计算 -> 裁定 -> 重开复核 -> 导出/删除/再导入。"""
import os

from fastapi.testclient import TestClient

from nmrwb.api import create_app
from nmrwb.core import Store

CASEPACK = os.path.join(os.path.dirname(__file__), "..", "casepack")


def _client(tmp_path, vclock):
    return TestClient(create_app(
        Store(str(tmp_path / "api.db"), clock=vclock), casepack_dir=CASEPACK))


def test_format_error_returns_400(tmp_path, vclock):
    c = _client(tmp_path, vclock)
    r = c.post("/api/inputs",
               json={"name": "h1", "format": "nmrwb-1d", "raw": "junk"})
    assert r.status_code == 400


def test_full_review_flow_and_roundtrip(tmp_path, vclock):
    c = _client(tmp_path, vclock)
    assert c.get("/").status_code == 200

    r = c.post("/api/casepack/load")
    assert r.status_code == 200
    st = c.get("/api/state").json()
    assert st["conflicts"]
    assert st["candidate_sets"]

    # 页面可做人工裁定：再否定一个候选 -> 新分支
    aset = list(st["candidate_sets"].values())[-1]
    cand = next(x for x in aset["candidates"] if x["atom_id"] == "H2")
    r = c.post("/api/decisions", json={
        "atom_id": "H2", "candidate_id": cand["candidate_id"],
        "action": "reject", "editor": "carol"})
    assert r.status_code == 200
    assert r.json()["payload"]["new_branch"].startswith("b")

    # 重开复核（刷新/新客户端）：数据仍在
    c2 = TestClient(c.app)
    st2 = c2.get("/api/state").json()
    assert any(d["editor"] == "carol"
               for d in st2["decisions"].values())

    # 历史重放
    first = st2["timeline"][0]["seq"]
    old = c2.get("/api/state", params={"seq": first}).json()
    assert len(old["timeline"]) == first

    # 导出 -> 删除本地数据库 -> 再导入：身份/顺序保持
    archive = c2.get("/api/export").text
    c2.post("/api/reset")
    assert c2.get("/api/state").json()["timeline"] == []
    r = c2.post("/api/import", json={"archive": archive})
    assert r.status_code == 200
    st3 = c2.get("/api/state").json()
    assert [e["event_id"] for e in st3["timeline"]] == \
           [e["event_id"] for e in st2["timeline"]]


def test_manual_import_compute_decide(tmp_path, vclock):
    c = _client(tmp_path, vclock)
    mol = "# nmrwb-mol v1\natom C1 C 20-25 @ 60 40\natom H1 H 0.8-1.0 @ 60 10\nbond C1 H1\n"
    h1 = ("# nmrwb-1d v1\nnucleus: 1H\nsolvent: CDCl3\nfrequency_mhz: 400.13\n"
          "temperature_k: 298.0\nreference: TMS\npeak 0.90 100 1.0 s\n")
    assert c.post("/api/inputs",
                  json={"name": "mol", "format": "nmrwb-mol", "raw": mol}).status_code == 200
    assert c.post("/api/inputs",
                  json={"name": "h1", "format": "nmrwb-1d", "raw": h1}).status_code == 200
    r = c.post("/api/compute")
    assert r.status_code == 200
    st = c.get("/api/state").json()
    cand = list(st["candidate_sets"].values())[-1]["candidates"][0]
    r = c.post("/api/decisions", json={
        "atom_id": cand["atom_id"], "candidate_id": cand["candidate_id"],
        "action": "confirm", "editor": "alice"})
    assert r.status_code == 200
    assert "impact" in r.json()["payload"]
