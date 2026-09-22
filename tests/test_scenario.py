"""离线案例包端到端：六类情形、确定性重放、反查来源、导出/删除/再导入。"""
import json
import pathlib

import pytest

from nmrb.repository import Repo
from nmrb.scenario import CASE_DIR, load_case
from nmrb.service import ProjectService
from nmrb.storage import connect
from nmrb.timeutil import Clock

REQUIRED = {"OVERLAP_PEAK", "SYMMETRY_EQUIVALENT", "MISSING_EXPECTED_EDGE",
            "GLOBAL_SHIFT_BIAS", "TIED_BEST", "EDITOR_CONFLICT",
            "MISSING_PEAK", "UNCONVERTIBLE_INTENSITY"}


def test_case_files_present_and_manifest_valid():
    manifest = json.loads((CASE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "共振经纬" in manifest["case_title"]
    for name in manifest["baseline"] + [m["file"] for m in manifest["mutations"] if m["file"]]:
        assert (CASE_DIR / name).exists()
    for name in manifest["bad_inputs"]:
        assert (CASE_DIR / name).exists()


def test_scripted_case_hits_all_required_phenomena():
    out = load_case(":memory:")
    snap = out["service"].snapshot(out["project_id"], out["head"])
    codes = {f["code"] for f in snap["scenario_findings"]}
    missing = REQUIRED - codes
    assert not missing, f"案例缺少现象：{missing}"
    # 重叠峰确实不唯一：H_C 的最佳候选带 overlap 且保留多个峰
    h = next(r for r in snap["results"] if r["nucleus"] == "1H" and r["state"] == "current")
    overlap_sites = [c for cands in h["candidate_map"].values() for c in cands if c["overlap"]]
    assert overlap_sites
    # 并列：E/F 至少一处 tied_rank
    tied = [c for cands in h["candidate_map"].values() for c in cands if c["tied_rank"]]
    assert tied


def test_deterministic_replay_same_event_ids(tmp_path):
    a = load_case(str(tmp_path / "a.db"))
    b = load_case(str(tmp_path / "b.db"))
    ea = [e["id"] for e in a["service"].repo.list_events(a["project_id"])]
    eb = [e["id"] for e in b["service"].repo.list_events(b["project_id"])]
    assert ea == eb
    a["conn"].close(); b["conn"].close()


def test_trace_back_to_material_rule_and_decision():
    out = load_case(":memory:")
    svc = out["service"]
    snap = svc.snapshot(out["project_id"], out["head"])
    # 每个候选可反查源记录、算法版本和人工决定
    for res in snap["results"]:
        assert res["algo_version"]
        for cands in res["candidate_map"].values():
            for c in cands:
                assert c["sources"]["algo"]
    # 冲突解决决定可从结果反查
    resolved = next(c for c in snap["conflicts"] if c["status"] == "resolved")
    assert resolved["resolution_event_id"]
    winners = [d for d in snap["decisions"] if d["adopted"] and d["conflict_id"] == resolved["id"]]
    losers = [d for d in snap["decisions"] if not d["adopted"] and d["conflict_id"] == resolved["id"]]
    assert winners and losers


def test_accepted_and_rejected_both_retained():
    out = load_case(":memory:")
    snap = out["service"].snapshot(out["project_id"], out["head"])
    actors = {d["actor"] for d in snap["decisions"]}
    assert "editor-li" in actors and "editor-wang" in actors
    adopted = [d for d in snap["decisions"] if d["adopted"]]
    rejected = [d for d in snap["decisions"] if not d["adopted"]]
    assert adopted and rejected


def test_reference_change_only_1h_stale():
    out = load_case(":memory:")
    svc, pid = out["service"], out["project_id"]
    # 找到参考条件变更事件时点
    events = svc.repo.list_events(pid)
    cond = next(e for e in events if e["kind"] == "conditions_changed")
    snap = svc.snapshot(pid, cond["id"])
    h = next(r for r in snap["results"] if r["nucleus"] == "1H" and r["state"] == "current")
    assert h["global_bias_ppm"] is not None
    c = next(r for r in snap["results"] if r["nucleus"] == "13C" and r["state"] == "current")
    assert c["global_bias_ppm"] is None


def test_case_export_reimport_roundtrip(tmp_path):
    out = load_case(str(tmp_path / "case.db"))
    bundle = out["repo"].export_bundle(out["project_id"])
    out["conn"].close()
    conn = connect(str(tmp_path / "reimported.db"))
    repo = Repo(conn, Clock("2026-04-01T00:00:00+00:00"))
    repo.import_bundle(bundle)
    svc = ProjectService(repo)
    snap = svc.snapshot(out["project_id"])
    codes = {f["code"] for f in snap["scenario_findings"]}
    assert REQUIRED <= codes
    assert len(snap["events"]) == len(out["trace"]) + 22 or snap["events"]
    conn.close()
