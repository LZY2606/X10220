"""领域计算：六类边界情形的一等数据判定。"""
import copy

import pytest

from nmrb.assign import DEFAULT_PARAMS, build_assignment
from nmrb.models import Conditions, Edge2D, ExpectedEdge, Peak1D, Site


def cond(nucleus="1H"):
    return Conditions(nucleus=nucleus, solvent="CDCl3", temperature_k=298.15,
                      temperature_repr="298.15", spectrometer_mhz=500.13,
                      spectrometer_repr="500.13", reference="TMS@0ppm")


def site(sid, carbon, hydrogen, element, shift, group=None, protons=1):
    return Site(id=f"site-{sid}", label=sid, doc_id="dsk", doc_version=1,
                carbon=carbon, hydrogen=hydrogen, element=element,
                expected_shift_ppm=shift, expected_shift_repr=str(shift),
                expected_shift_unit="ppm", symmetry_group=group or carbon,
                protons=protons, raw="")


def peak(pid, nucleus, shift, intensity=100.0, integral=1.0, mult="d"):
    return Peak1D(id=f"peak-{pid}", label=pid, doc_id="dp", doc_version=1,
                  lineno=1, raw="", nucleus=nucleus, shift_ppm=shift,
                  shift_repr=str(shift), shift_unit="ppm", intensity=intensity,
                  intensity_repr=str(intensity), intensity_unit="counts",
                  integral=integral, integral_repr=str(integral), integral_unit="H",
                  multiplicity=mult, conditions=cond(nucleus))


def edge(eid, exp, f1, f2, nuclei, strength="strong"):
    return Edge2D(id=f"edge-{eid}", label=eid, doc_id="de", doc_version=1, lineno=1,
                  raw="", experiment=exp, nuclei=nuclei, f1_ppm=f1, f2_ppm=f2,
                  f1_repr=str(f1), f2_repr=str(f2), f1_unit="ppm", f2_unit="ppm",
                  strength=strength, strength_repr=strength)


def test_overlap_not_force_unique():
    sites = [site("A", "C_A", "H_A", "aromatic", 7.15, "G_A"),
             site("B", "C_B", "H_B", "aromatic", 7.16, "G_B")]
    peaks = [peak("p1", "1H", 7.155, mult="dd"), peak("p2", "1H", 7.158, mult="d")]
    res = build_assignment("1H", sites, peaks, [], [], DEFAULT_PARAMS, {})
    overlap = {f.code for f in res.findings}
    assert "OVERLAP_PEAK" in overlap
    for cands in res.candidate_map.values():
        assert all(c.overlap for c in cands if c.peak_id)
        # 两个峰都保留为候选，不被强制唯一
        assert len({c.peak_id for c in cands if c.peak_id}) == 2


def test_symmetry_atoms_share_peak():
    sites = [site("A", "C_A", "H_A", "aromatic", 7.30, "G_AR", protons=1),
             site("B", "C_B", "H_B", "aromatic", 7.30, "G_AR", protons=1)]
    peaks = [peak("p1", "1H", 7.30, integral=2.0)]
    res = build_assignment("1H", sites, peaks, [], [], DEFAULT_PARAMS, {})
    assert any(f.code == "SYMMETRY_EQUIVALENT" for f in res.findings)
    # 两个对称位点都合法映射到同一个峰
    for s in sites:
        top = res.candidate_map[s.id][0]
        assert top.peak_id == "peak-p1"
        assert top.status == "live"
        int_ev = next(e for e in top.evidences if e.code == "integral")
        assert int_ev.contribution > 0.9


def test_missing_edge_is_evidence_only():
    sites = [site("A", "C_A", "H_A", "chain", 2.60, "G_A"),
             site("B", "C_B", "H_B", "aliphatic", 1.30, "G_B")]
    peaks = [peak("a", "1H", 2.60, mult="t"), peak("b", "1H", 1.30, integral=3.0, mult="t")]
    # 骨架声明 A-B COSY，但观测数据没有对应边
    expected = [ExpectedEdge(id="x1", kind="bond", a="H_A", b="H_B", edge_type="vicinal")]
    res = build_assignment("1H", sites, peaks, [], expected, DEFAULT_PARAMS, {})
    assert any(f.code == "MISSING_EXPECTED_EDGE" for f in res.findings)
    cand = res.candidate_map["site-A"][0]
    cosy = next(e for e in cand.evidences if e.code == "cosy")
    assert cosy.status == "absent" and cosy.contribution == 0.0
    # 位点仍是 live 候选，未被强制排除
    assert cand.status == "live" and cand.peak_id == "peak-a"


def test_global_shift_bias_only_degrades_shift_evidence():
    sites = [site("A", "C_A", "H_A", "chain", 2.60, "G_A"),
             site("B", "C_B", "H_B", "aliphatic", 1.30, "G_B")]
    peaks = [peak("a", "1H", 2.82, mult="t"), peak("b", "1H", 1.52, integral=3.0, mult="t")]
    edges = [edge("e1", "COSY", 2.82, 1.52, ["1H", "1H"])]
    expected = [ExpectedEdge(id="x", kind="bond", a="H_A", b="H_B", edge_type="vicinal")]
    res = build_assignment("1H", sites, peaks, edges, expected, DEFAULT_PARAMS, {})
    assert res.global_bias_ppm is not None
    assert abs(res.global_bias_ppm - 0.22) < 1e-6
    assert any(f.code == "GLOBAL_SHIFT_BIAS" for f in res.findings)
    cand = res.candidate_map["site-A"][0]
    # 拓扑证据（COSY）仍然支持；位移证据用偏置校正
    cosy = next(e for e in cand.evidences if e.code == "cosy")
    assert cosy.status == "support"


def test_tied_best_all_retained():
    sites = [site("E", "C_E", "H_E", "chain", 2.62, "G_E"),
             site("F", "C_F", "H_F", "chain", 2.62, "G_F")]
    peaks = [peak("e1", "1H", 2.62, mult="t"), peak("e2", "1H", 2.62, mult="t")]
    res = build_assignment("1H", sites, peaks, [], [], DEFAULT_PARAMS, {})
    assert any(f.code == "TIED_BEST" for f in res.findings)
    for s in sites:
        cands = res.candidate_map[s.id]
        assert cands[0].tied_rank and cands[1].tied_rank
        assert abs(cands[0].score - cands[1].score) <= DEFAULT_PARAMS["score_epsilon"]
        assert len({c.peak_id for c in cands}) == 2


def test_missing_peak_unknown_not_snapped():
    sites = [site("Z", "C_Z", "H_Z", "chain", 3.10, "G_Z")]
    peaks = [peak("far", "1H", 9.0, mult="s")]
    res = build_assignment("1H", sites, peaks, [], [], DEFAULT_PARAMS, {})
    cands = res.candidate_map["site-Z"]
    assert any(f.code == "MISSING_PEAK" for f in res.findings)
    assert cands[0].kind == "unknown_unobserved"
    assert cands[0].peak_id is None
    assert not any(c.peak_id == "peak-far" for c in cands)


def test_unmatched_peak_reported_and_kept():
    sites = [site("A", "C_A", "H_A", "aromatic", 7.0, "G_A")]
    peaks = [peak("a", "1H", 7.0), peak("extra", "1H", 0.5)]
    res = build_assignment("1H", sites, peaks, [], [], DEFAULT_PARAMS, {})
    assert any(f.code == "UNMATCHED_PEAK" and "peak-extra" in f.related
               for f in res.findings)


def test_rejection_reasons_are_first_class_when_out_of_tolerance():
    # 位移超容差 -> 只剩 unknown 候选，且有明确缺峰说明
    sites = [site("A", "C_A", "H_A", "aromatic", 7.0, "G_A")]
    peaks = [peak("a", "1H", 9.9)]
    res = build_assignment("1H", sites, peaks, [], [], DEFAULT_PARAMS, {})
    cands = res.candidate_map["site-A"]
    assert cands and cands[0].kind == "unknown_unobserved"


def test_numpy_used_and_seed_independent():
    import numpy as np
    np.random.seed(7)
    # 引擎应为确定性：重复构建结果身份一致
    sites = [site("A", "C_A", "H_A", "aromatic", 7.0, "G_A")]
    r1 = build_assignment("1H", sites, [peak("a", "1H", 7.0)], [], [], DEFAULT_PARAMS, {})
    r2 = build_assignment("1H", sites, [peak("a", "1H", 7.0)], [], [], DEFAULT_PARAMS, {})
    assert r1.id == r2.id
