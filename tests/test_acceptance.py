"""验收：跑通仓库自带“NMR 共振经纬”离线案例包的六类边界情形。"""
import os

from nmrwb import services
from nmrwb.casepack import load_casepack

CASEPACK = os.path.join(os.path.dirname(__file__), "..", "casepack")


def _load(store):
    load_casepack(store, CASEPACK)
    return services.current_state(store)


def _cand(state, atom, shift):
    for s in state["candidate_sets"].values():
        for c in s["candidates"]:
            if c["atom_id"] == atom and c["shift_ppm"] == shift:
                return c
    raise AssertionError("候选不存在: %s %s" % (atom, shift))


def test_overlapping_peaks_keep_all_legal_candidates(store):
    st = _load(store)
    h1 = [c for s in st["candidate_sets"].values()
          for c in s["candidates"] if c["atom_id"] == "H1"]
    shifts = {c["shift_ppm"] for c in h1}
    assert {0.86, 0.88} <= shifts          # 重叠峰都保留，不唯一化
    assert any("overlap" in c["flags"] for c in h1)


def test_symmetric_atoms_and_tied_best(store):
    st = _load(store)
    for aid in ("C1", "C1b"):
        best = [c for s in st["candidate_sets"].values()
                for c in s["candidates"]
                if c["atom_id"] == aid and c["shift_ppm"] == 22.35
                and c["status"] == "best"]
        assert best                          # 对称核在同一峰上并列最优
        assert any("symmetry" in c["flags"] for c in best)


def test_missing_edge_change_scoped_invalidation(store):
    _load(store)
    # 找到 hmbc v2 提交事件，重放到该时刻（尚未用新版本重新计算）
    hmbc_v2_seq = None
    for e in store.events():
        if (e["kind"] == "input_submitted"
                and e["payload"]["name"] == "hmbc"
                and e["payload"]["version"] == 2):
            hmbc_v2_seq = e["seq"]
    assert hmbc_v2_seq is not None
    st = services.current_state(store, upto=hmbc_v2_seq)
    aset = list(st["candidate_sets"].values())[-1]
    stale = {(c["atom_id"], c["shift_ppm"]): c["stale"]
             for c in aset["candidates"]}
    assert stale[("C2", 30.6)] is True      # 以缺失 HMBC 边为证据 -> 过期
    assert stale[("H4", 0.88)] is False     # 不依赖该边 -> 仍当前


def test_global_shift_bias_diagnostic(store):
    st = _load(store)
    latest = list(st["candidate_sets"].values())[-1]
    diag = [d for d in latest["diagnostics"]
            if d["type"] == "global_shift_bias"]
    assert diag and diag[0]["nucleus"] == "1H"


def test_two_editor_conflict_resolved_and_undone(store):
    st = _load(store)
    assert st["conflicts"], "应检测到两个编辑者冲突"
    c = list(st["conflicts"].values())[0]
    assert c["status"] == "resolved"
    assert set(c["editors"]) == {"alice", "bao"}
    undone = [d for d in st["decisions"].values() if d.get("undone")]
    assert undone and undone[0]["editor"] in (None, "alice")


def test_decisions_create_branches(store):
    st = _load(store)
    decisions = list(st["decisions"].values())
    assert len(decisions) == 2
    assert all(d["new_branch"] in st["branches"] for d in decisions)


def test_every_result_traces_to_source(store):
    st = _load(store)
    latest = list(st["candidate_sets"].values())[-1]
    edge_ids = {e["edge_id"]
                for v in st["input_versions"].values()
                if v["format"] == "nmrwb-2d"
                for e in v["parsed"]["edges"]}
    for c in latest["candidates"]:
        assert all(d in st["input_versions"] or d in
                   {p["params_id"] for p in st["params"]}
                   for d in c["depends_on"])
        for ev in c["evidence"]:
            assert ev["edge_id"] in edge_ids


def test_archive_export_reimport_roundtrip(store):
    _load(store)
    text = store.export_jsonl()
    from nmrwb.core import Store
    s2 = Store(":memory:")
    n = s2.import_jsonl(text)
    assert n == len(store.events())
    a = services.current_state(store)
    b = services.current_state(s2)
    assert [e["event_id"] for e in a["timeline"]] == \
           [e["event_id"] for e in b["timeline"]]
    assert list(a["candidate_sets"]) == list(b["candidate_sets"])


def test_versions_keep_conditions_and_units(store):
    st = _load(store)
    h1_versions = [v for v in st["input_versions"].values()
                   if v["name"] == "h1"]
    assert len(h1_versions) == 2
    v1 = next(v for v in h1_versions if v["version"] == 1)
    assert v1["parsed"]["meta"]["frequency_mhz"] == "400.13"
    assert v1["parsed"]["meta"]["solvent"] == "CDCl3"
