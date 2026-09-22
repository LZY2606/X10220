import numpy as np

from nmrwb.core import DEFAULT_PARAMS
from nmrwb.engine import compute_candidates
from nmrwb.ingest import parse_input

np.random.seed(0)

MOL = """# nmrwb-mol v1
atom C1 C 20.0-25.0 sym=methyl @ 60 40
atom C1b C 20.0-25.0 sym=methyl @ 60 120
atom C2 C 28.0-33.0 @ 150 105
atom C3 C 39.0-45.0 @ 240 105
atom H1 H 0.70-1.00 sym=methylH @ 20 30
atom H1b H 0.70-1.00 sym=methylH @ 20 180
atom H2 H 1.35-1.65 @ 150 55
atom H3 H 1.05-1.35 @ 240 155
bond C1 C2
bond C1b C2
bond C2 C3
bond C1 H1
bond C1b H1b
bond C2 H2
bond C3 H3
"""

H1 = """# nmrwb-1d v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
temperature_k: 298.0
reference: TMS
peak 0.86 1000 6.0 d
peak 0.88 940 3.0 t
peak 1.21 610 2.0 m
peak 1.50 320 1.0 m
"""

C13 = """# nmrwb-1d v1
nucleus: 13C
solvent: CDCl3
frequency_mhz: 100.61
temperature_k: 298.0
reference: TMS
peak 22.35 620 2.0 q
peak 30.6 340 1.0 d
peak 41.8 380 1.0 t
"""

COSY = """# nmrwb-2d v1
type: COSY
edge 0.86 1.50 medium
edge 1.50 1.21 medium
"""


def mk(triples):
    inputs, deps = {}, {}
    for name, fmt, raw in triples:
        inputs[name] = {"format": fmt, "parsed": parse_input(name, fmt, raw)["parsed"]}
        deps[name] = "input_%s" % name
    return inputs, deps


def cands(result, atom=None):
    return [c for c in result["candidates"] if atom is None or c["atom_id"] == atom]


def test_overlapping_peaks_not_force_matched():
    inputs, deps = mk([("mol", "nmrwb-mol", MOL), ("h1", "nmrwb-1d", H1),
                       ("c13", "nmrwb-1d", C13)])
    r = compute_candidates(inputs, DEFAULT_PARAMS, deps, "params_v1")
    h1 = cands(r, "H1")
    assert len({c["peak_id"] for c in h1}) == 2        # 两个重叠峰都保留
    assert all("overlap" in c["flags"] for c in h1)
    # 同一峰同时合法属于 H1/H1b/H4 等多个核，不被强制唯一
    assert any("shared-peak" in c["flags"] for c in r["candidates"])


def test_symmetric_atoms_marked_interchangeable():
    inputs, deps = mk([("mol", "nmrwb-mol", MOL), ("c13", "nmrwb-1d", C13)])
    r = compute_candidates(inputs, DEFAULT_PARAMS, deps, "params_v1")
    for aid in ("C1", "C1b"):
        assert any("symmetry" in c["flags"] for c in cands(r, aid))


def test_missing_correlation_edge_is_evidence_only():
    inputs, deps = mk([("mol", "nmrwb-mol", MOL), ("h1", "nmrwb-1d", H1),
                       ("c13", "nmrwb-1d", C13)])       # 没有任何 COSY
    r = compute_candidates(inputs, DEFAULT_PARAMS, deps, "params_v1")
    assert any(d["type"] == "missing_edge" for d in r["diagnostics"])
    flagged = [c for c in r["candidates"] if "missing-edge" in c["flags"]]
    assert flagged                                    # 有缺边标记
    assert cands(r, "H1")                             # 候选仍在，未被排除


def test_global_shift_bias_detected_not_silently_fixed():
    biased = H1.replace("0.86 1000", "0.92 1000").replace(
        "0.88 940", "0.94 940").replace("1.21 610", "1.27 610").replace(
        "1.50 320", "1.56 320")
    inputs, deps = mk([("mol", "nmrwb-mol", MOL), ("h1", "nmrwb-1d", biased),
                       ("c13", "nmrwb-1d", C13), ("cosy", "nmrwb-2d", COSY)])
    r = compute_candidates(inputs, DEFAULT_PARAMS, deps, "params_v1")
    d = [x for x in r["diagnostics"] if x["type"] == "global_shift_bias"]
    assert d and d[0]["nucleus"] == "1H"
    assert abs(d[0]["value_ppm"] - 0.06) < 0.02


def test_tied_best_candidates_kept():
    mol = """# nmrwb-mol v1
atom C1 C 20.0-25.0 @ 60 40
atom H1 H 0.80-0.90 @ 60 10
bond C1 H1
"""
    h1 = """# nmrwb-1d v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
temperature_k: 298.0
reference: TMS
peak 0.83 100 1.0 d
peak 0.87 100 1.0 d
"""
    inputs, deps = mk([("mol", "nmrwb-mol", mol), ("h1", "nmrwb-1d", h1)])
    r = compute_candidates(inputs, DEFAULT_PARAMS, deps, "params_v1")
    best = cands(r, "H1")
    best = [c for c in best if c["status"] == "best"]
    assert len(best) == 2                            # 并列最优，不唯一化


def test_every_candidate_carries_provenance():
    inputs, deps = mk([("mol", "nmrwb-mol", MOL), ("h1", "nmrwb-1d", H1),
                       ("c13", "nmrwb-1d", C13), ("cosy", "nmrwb-2d", COSY)])
    r = compute_candidates(inputs, DEFAULT_PARAMS, deps, "params_v1")
    for c in r["candidates"]:
        assert "input_mol" in c["depends_on"]
        assert "params_v1" in c["depends_on"]
        assert c["candidate_id"].startswith("cand_")
