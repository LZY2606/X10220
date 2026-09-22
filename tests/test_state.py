"""核心状态机：版本不可变、局部失效、历史重放、冲突与导出/再导入。"""
import copy

from nmrb.assign import DEFAULT_PARAMS
from nmrb.scenario import BASE_FILES, load_case
from nmrb.service import ProjectService
from nmrb.storage import connect
from nmrb.repository import Repo
from nmrb.timeutil import Clock


SKEL = """@format NMRB-SKELETON/1
@s1 C_A H_A aromatic 7.30ppm group=G_A
@s2 C_G H_G aliphatic 1.28ppm group=G_G protons=3
@s3 C_A - carbon 128.5ppm group=G_A
@s4 C_G - carbon 15.4ppm group=G_G
@b1 bond H_A H_G vicinal
"""
P1H = """@format NMRB-PEAKS1/1
#nucleus 1H
#solvent CDCl3
#temperature-k 298.15
#spectrometer-mhz 500.13
#reference TMS@0ppm
@ha 7.30 100 1.0 d
@hg 1.28 120 3.0 t
"""
P13C = """@format NMRB-PEAKS1/1
#nucleus 13C
#solvent CDCl3
#temperature-k 298.15
#spectrometer-mhz 500.13
#reference TMS@0ppm
@ca 128.5 40 1 s
@cg 15.4 28 1 s
"""
COSY = """@format NMRB-PEAKS2/1
#experiment COSY
#nuclei 1H,1H
@e1 7.30 1.28 strong
"""
HSQC = """@format NMRB-PEAKS2/1
#experiment HSQC
#nuclei 1H,13C
@q1 7.30 128.5 strong
@q2 1.28 15.4 strong
"""
HMBC = "@format NMRB-PEAKS2/1\n#experiment HMBC\n#nuclei 1H,13C\n"


def seed(svc, pid="p1"):
    parent = None
    for name, text in [("m.skel", SKEL), ("h.pk1", P1H), ("c.pk1", P13C),
                       ("cosy.peak2", COSY), ("hsqc.peak2", HSQC),
                       ("hmbc.peak2", HMBC)]:
        parent = svc.import_source(pid, name, text, "importer", f"导入 {name}", parent)["event"]["id"]
    return parent


def current_result(snap, nucleus):
    return next(r for r in snap["results"] if r["nucleus"] == nucleus and r["state"] == "current")


def test_raw_inputs_immutable_and_versioned(service):
    pid = "p1"
    head = seed(service, pid)
    docs = service.repo.list_documents(pid)
    hdoc = next(d for d in docs if d["logical_name"] == "h.pk1")
    assert hdoc["version"] == 1
    old_records = {r["id"]: r["raw"] for r in hdoc["records"]}

    new_text = P1H.replace("@hg 1.28 120", "@hg 1.20 120")
    out = service.import_source(pid, "h.pk1", new_text, "editor", "局部改 H_G", head)
    assert out["version"] == 2 and out["commit_event"]["id"] != out["event"]["id"]

    # v1 仍可完整取回，内容未被覆盖
    v1 = service.repo.get_documents_at(pid, {hdoc["doc_id"]: 1})[0]
    assert v1["raw_text"] == P1H
    v2 = service.repo.get_documents_at(pid, {hdoc["doc_id"]: 2})[0]
    assert "@hg 1.20" in v2["raw_text"]

    # 未改动的记录身份稳定（H_A），改动行身份改变（H_G）
    v2_map = {r["label"]: r["id"] for r in v2["records"]}
    v1_map = {r["label"]: r["id"] for r in v1["records"]}
    assert v1_map["@ha"] == v2_map["@ha"]
    assert v1_map["@hg"] != v2_map["@hg"]


def test_local_change_only_invalidates_dependent_result(service):
    pid = "p1"
    head = seed(service, pid)
    snap1 = service.snapshot(pid, head)
    h1 = current_result(snap1, "1H")
    c1 = current_result(snap1, "13C")

    # 只改 1H 一条峰
    new_text = P1H.replace("@hg 1.28 120", "@hg 1.20 120")
    out = service.import_source(pid, "h.pk1", new_text, "editor", "局部改 H_G", head)
    new_head = out["event"]["id"]
    snap2 = service.snapshot(pid, new_head)
    h2 = current_result(snap2, "1H")
    c2 = current_result(snap2, "13C")
    assert h2["id"] != h1["id"]
    # 13C 未依赖被改记录，结果身份不变（不被误判过期）
    assert c2["id"] == c1["id"]

    # 旧结果仍可查看但标记 historical，不能冒充当前
    old_h = next(r for r in snap2["results"] if r["id"] == h1["id"])
    assert old_h["state"] == "historical"
    assert h2["state"] == "current"


def test_missing_peak_only_adds_unknown_for_affected_site(service):
    pid = "p1"
    head = seed(service, pid)
    text = "\n".join(line for line in P1H.splitlines() if not line.startswith("@hg"))
    out = service.import_source(pid, "h.pk1", text, "editor", "删除 H_G", head)
    snap = service.snapshot(pid, out["event"]["id"])
    h = current_result(snap, "1H")
    g_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_G")
    a_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_A")
    assert h["candidate_map"][g_site][0]["kind"] == "unknown_unobserved"
    assert h["candidate_map"][a_site][0]["kind"] == "assignment"


def test_reference_change_expires_only_related_nucleus(service):
    pid = "p1"
    head = seed(service, pid)
    biased = P1H.replace("7.30", "7.52").replace("1.28", "1.50").replace(
        "#reference TMS@0ppm", "#reference LOCK@0.22ppm-offset")
    out = service.change_conditions(pid, "h.pk1", "reference", biased,
                                    "editor", head)
    snap = service.snapshot(pid, out["event"]["id"])
    h = current_result(snap, "1H")
    c = current_result(snap, "13C")
    assert h["global_bias_ppm"] is not None and abs(h["global_bias_ppm"] - 0.22) < 1e-6
    # 13C 不受溶剂/参考变化影响
    snap_before = service.snapshot(pid, head)
    c_before = current_result(snap_before, "13C")
    assert c["id"] == c_before["id"]
    assert any(e["kind"] == "conditions_changed" for e in snap["events"])


def test_history_checkout_replays_old_versions(service):
    pid = "p1"
    head = seed(service, pid)
    h_before = current_result(service.snapshot(pid, head), "1H")
    p2 = copy.deepcopy(DEFAULT_PARAMS)
    p2["shift_tolerance_ppm"]["1H"] = 0.10
    rev = service.revise_params(pid, p2, "li", "收紧容差", head)
    h_after = current_result(service.snapshot(pid, rev["event"]["id"]), "1H")
    assert h_after["id"] != h_before["id"]

    # 回到旧事件：看到的是当时的结论，不被最新计算覆盖
    old_snap = service.snapshot(pid, head)
    shown = next(r for r in old_snap["results"] if r["id"] == h_before["id"])
    assert shown["state"] == "current"
    newer = next(r for r in old_snap["results"] if r["id"] == h_after["id"])
    assert newer["state"] in ("stale", "historical")


def _seed_tie(service, pid="p1"):
    skel = """@format NMRB-SKELETON/1
@s1 C_E H_E chain 2.62ppm group=G_E
@s2 C_F H_F chain 2.62ppm group=G_F
@s3 C_E - carbon 35.6ppm group=G_E
@s4 C_F - carbon 35.9ppm group=G_F
@b1 bond H_E H_F vicinal
"""
    p1h = """@format NMRB-PEAKS1/1
#nucleus 1H
#solvent CDCl3
#spectrometer-mhz 500.13
#reference TMS@0ppm
@e1 2.62 60 1 t
@e2 2.62 60 1 t
"""
    p13c = """@format NMRB-PEAKS2/1
#experiment HSQC
#nuclei 1H,13C
@q1 2.62 35.6 strong
@q2 2.62 35.9 strong
"""
    parent = None
    for name, text in [("s.skel", skel), ("h.pk1", p1h)]:
        parent = service.import_source(pid, name, text, "importer", "x", parent)["event"]["id"]
    return parent


def test_two_editors_conflict_and_resolution_keeps_both(service):
    pid = "p1"
    head = _seed_tie(service, pid)
    snap = service.snapshot(pid, head)
    res = current_result(snap, "1H")
    e_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_E")
    peaks = [c["peak_id"] for c in res["candidate_map"][e_site]]
    assert len(peaks) == 2 and any(c["tied_rank"] for c in res["candidate_map"][e_site])

    d1 = service.decide(pid, e_site, peaks[0], res["id"], "accept", "editor-li",
                        "李的选择", head, branch="review-li")
    d2 = service.decide(pid, e_site, peaks[1], res["id"], "accept", "editor-wang",
                        "王的选择", head, branch="review-wang")
    cid = service.detect_conflict(pid, e_site, d1["decision"]["decision_id"],
                                  d2["decision"]["decision_id"], head)
    assert cid
    findings = {f["code"] for f in service.repo.list_scenario_findings(pid)}
    assert "EDITOR_CONFLICT" in findings

    resolved = service.resolve(pid, cid, d1["decision"]["decision_id"],
                               d2["decision"]["decision_id"], "lead", "裁决",
                               d1["decision"]["event"]["id"], "review-li")
    snap2 = service.snapshot(pid, resolved["resolution_event"]["id"])
    conflict = snap2["conflicts"][0]
    assert conflict["status"] == "resolved"
    decisions = {d["actor"]: d["adopted"] for d in snap2["decisions"]}
    assert decisions["editor-li"] == 1
    assert decisions["editor-wang"] == 0
    # 王的未采纳分支仍保留，可从事件树访问
    wang_head = d2["decision"]["event"]["id"]
    wang_snap = service.snapshot(pid, wang_head)
    assert any(d["actor"] == "editor-wang" for d in wang_snap["decisions"])


def test_undo_writes_history(service):
    pid = "p1"
    head = _seed_tie(service, pid)
    snap = service.snapshot(pid, head)
    res = current_result(snap, "1H")
    e_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_E")
    peak_id = res["candidate_map"][e_site][0]["peak_id"]
    d = service.decide(pid, e_site, peak_id, res["id"], "accept", "li", "x",
                       head, branch="review-li")
    undo = service.undo(pid, d["decision"]["event"]["id"], "li",
                        d["recomputed_event"]["id"], "review-li")
    assert undo["undo_event"]["kind"] == "decision_undone"
    events = service.repo.list_events(pid)
    target = next(e for e in events if e["id"] == d["decision"]["event"]["id"])
    assert target["undone"] is True


def test_export_delete_reimport_preserves_identity_and_order(tmp_path, service):
    pid = "p1"
    head = seed(service, pid)
    bundle = service.repo.export_bundle(pid)

    # 删除数据库（使用全新连接与文件）模拟
    new_db = str(tmp_path / "fresh.db")
    conn2 = connect(new_db)
    repo2 = Repo(conn2, Clock("2026-03-02T00:00:00+00:00"))
    repo2.import_bundle(bundle)
    svc2 = ProjectService(repo2)
    snap2 = svc2.snapshot(pid, head)

    # 项目、事件顺序、文档/记录身份保持
    assert repo2.get_project(pid)["name"] == "测试项目"
    seqs = [e["seq"] for e in snap2["events"]]
    assert seqs == sorted(seqs)
    docs1 = {d["doc_id"]: d["records"] for d in service.snapshot(pid, head)["documents"]}
    docs2 = {d["doc_id"]: d["records"] for d in snap2["documents"]}
    assert set(docs1) == set(docs2)
    for doc_id in docs1:
        ids1 = [r["id"] for r in docs1[doc_id]]
        ids2 = [r["id"] for r in docs2[doc_id]]
        assert ids1 == ids2

    # 结果身份（算法+参数+直接来源指纹）保持
    r1 = {(r["nucleus"], r["state"]): r["id"] for r in service.snapshot(pid, head)["results"]}
    r2 = {(r["nucleus"], r["state"]): r["id"] for r in snap2["results"]}
    for key in (("1H", "current"), ("13C", "current")):
        assert r1[key] == r2[key]
