"""Domain engine: resonance candidates, evidence and findings.

The engine never forces a unique assignment.
  * Overlapping / degenerate peaks may legally serve several nuclei; a
    nucleus with no observable peak becomes an *unknown* candidate.
  * The coupling network (COSY/HSQC/HMBC) is scored evidence only; a missing
    edge weakens a candidate, an extra edge is recorded, neither is fatal.
  * Global shift bias is detected (median residual) and, only when the rule
    pack enables it, corrected -- never silently.
  * Equally optimal candidates are both reported as ties.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .ids import stable_id
from .models import (finding, FINDING_BIAS, FINDING_MISSING_EDGE,
                     FINDING_MISSING_PEAK, FINDING_OVERLAP,
                     FINDING_SYMMETRY, FINDING_TIE, SEV_INFO, SEV_WARN,
                     KIND_COSY, KIND_HSQC, KIND_HMBC, CAND_UNKNOWN,
                     CAND_REJECTED, CAND_VIABLE)
from . import rules

ENGINE_VERSION = "engine-1.0.0"
MULT_NEIGH = {"s": 0, "d": 1, "t": 2, "q": 3, "sext": 5, "sept": 6,
              "oct": 7}


def run_build(structure_doc, rule_config: Dict[str, Any],
              spectra: Dict[str, Any], conditions: Dict[str, str],
              source_refs: Dict[str, str], algorithm_version: str = ENGINE_VERSION
              ) -> Dict[str, Any]:
    """Compute one reproducible candidate build.

    spectra: {doc_role: parsed_spectrum_doc}; roles are h1, c13, cosy, hsqc,
    hmbc.  source_refs maps role -> source_id for provenance.
    """
    atoms = structure_doc.atoms
    bonds = structure_doc.bonds
    equiv = structure_doc.equiv_groups
    adjacency = _adjacency(atoms, bonds)
    h1_doc = spectra.get("h1")
    c_doc = spectra.get("c13")
    nuclei = _enumerate_nuclei(atoms, adjacency, equiv, rule_config)
    proton_peaks = [dict(p, role="h1") for p in (h1_doc.peaks if h1_doc else [])]
    carbon_peaks = [dict(p, role="c13") for p in (c_doc.peaks if c_doc else [])]
    proton_peaks = [p for p in proton_peaks if p.get("shift_ppm") is not None]
    carbon_peaks = [p for p in carbon_peaks if p.get("shift_ppm") is not None]

    edge_sets = {}
    for role, kind in (("cosy", KIND_COSY), ("hsqc", KIND_HSQC),
                       ("hmbc", KIND_HMBC)):
        doc = spectra.get(role)
        if doc is not None:
            edge_sets[kind] = _resolve_edges(doc, proton_peaks, carbon_peaks,
                                             rule_config, kind)

    findings: List[Dict[str, Any]] = []
    proton_peaks = _annotate_overlaps(proton_peaks, rule_config, findings)
    carbon_peaks = _annotate_overlaps(carbon_peaks, rule_config, findings,
                                      nucleus_label="13C")
    _symmetry_findings(equiv, findings)

    # Global shift bias per nucleus, computed against nearest expected shift.
    h_bias = _detect_bias(nuclei, proton_peaks, "1H", rule_config, findings)
    c_bias = _detect_bias([n for n in nuclei if n["nucleus"] == "13C"],
                          carbon_peaks, "13C", rule_config, findings)

    expected_edges = _expected_network(nuclei, adjacency)
    candidates = []
    per_nucleus: Dict[str, List[Dict[str, Any]]] = {}
    pools = {"1H": proton_peaks, "13C": carbon_peaks}
    for nuc in nuclei:
        bias = h_bias if nuc["nucleus"] == "1H" else c_bias
        nuc_cands = _candidates_for(nuc, pools, nuclei, edge_sets,
                                    expected_edges, rule_config, bias)
        per_nucleus[nuc["id"]] = nuc_cands
        candidates.extend(nuc_cands)

    _missing_edge_findings(per_nucleus, edge_sets, expected_edges, nuclei,
                           findings)
    _unknown_nuclei(nuclei, per_nucleus, candidates, findings)
    _tie_findings(nuclei, per_nucleus, rule_config, findings)

    # Extra observed edges that support no candidate strongly are evidence,
    # never assignment errors.
    extra_edges = _extra_edges(edge_sets, per_nucleus, nuclei)

    return {
        "engine_version": algorithm_version,
        "rulepack_fingerprint": rule_config["fingerprint"],
        "rulepack_version": rule_config["rulepack_version"],
        "conditions": dict(conditions),
        "nuclei": nuclei,
        "proton_peaks": proton_peaks,
        "carbon_peaks": carbon_peaks,
        "edges": [e for kind in ("cosy", "hsqc", "hmbc")
                  for e in edge_sets.get(kind, [])],
        "candidates": candidates,
        "findings": findings,
        "extra_edges": extra_edges,
        "bias": {"1H": h_bias, "13C": c_bias},
        "source_refs": source_refs,
    }


def _adjacency(atoms: List[Dict[str, Any]],
               bonds: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    adj = {a["id"]: [] for a in atoms}
    for bond in bonds:
        adj.setdefault(bond["a"], []).append(bond["b"])
        adj.setdefault(bond["b"], []).append(bond["a"])
    return adj


def _enumerate_nuclei(atoms, adjacency, equiv, cfg) -> List[Dict[str, Any]]:
    """Enumerate assignment targets.

    Each attached hydrogen is one 1H nucleus id ``H<k>@Cx``; heavy atoms with
    h_count>0 are represented as one proton per hydrogen, grouped by @equiv
    into symmetry equivalence classes.  Each carbon is a 13C nucleus.
    """
    equiv_of: Dict[str, str] = {}
    groups: Dict[str, List[str]] = {}
    for group in equiv:
        groups[group["id"]] = list(group["members"])
        for member in group["members"]:
            equiv_of[member] = group["id"]
    nuclei: List[Dict[str, Any]] = []
    proton_index: Dict[str, List[str]] = {}
    for atom in atoms:
        if atom["element"] not in ("C",):
            continue
        cid = atom["id"]
        nuclei.append({
            "id": cid,
            "nucleus": "13C",
            "atom": cid,
            "label": atom["label"],
            "expected_ppm": cfg["expected_shifts"].get(cid),
            "equiv_group": equiv_of.get(cid),
            "ord": len(nuclei),
        })
        hc = atom.get("h_count") or 0
        proton_index[cid] = []
        for k in range(hc):
            hid = ("H@%s" % cid) if hc == 1 else "H%d@%s" % (k + 1, cid)
            proton_index[cid].append(hid)
            nuclei.append({
                "id": hid,
                "nucleus": "1H",
                "atom": cid,
                "label": atom["label"],
                "expected_ppm": cfg["expected_shifts"].get(hid),
                "equiv_group": equiv_of.get(cid),
                "ord": len(nuclei),
            })
    # Molecular graph for evidence: hydrogen inherits its carbon neighbours.
    for nuc in nuclei:
        nuc["carbon_neighbours"] = adjacency.get(nuc["atom"], [])
        if nuc["nucleus"] == "1H":
            nuc["proton_neighbours"] = [
                hid for cid in adjacency.get(nuc["atom"], [])
                for hid in proton_index.get(cid, [])]
        else:
            nuc["proton_neighbours"] = proton_index.get(nuc["atom"], [])
    return nuclei


def _match_peak_id(rid: str, peaks: List[Dict[str, Any]]
                   ) -> Optional[Dict[str, Any]]:
    for p in peaks:
        if p["id"] == rid:
            return p
    return None


def _nearest(peaks: List[Dict[str, Any]], shift: Optional[float],
             tol: float) -> Optional[Dict[str, Any]]:
    if shift is None or not peaks:
        return None
    best = min(peaks, key=lambda p: abs(p["shift_ppm"] - shift))
    if abs(best["shift_ppm"] - shift) <= tol:
        return best
    return None


def _resolve_edges(doc, proton_peaks, carbon_peaks, cfg, kind
                   ) -> List[Dict[str, Any]]:
    """Resolve 2D edges to 1D peak ids; unresolved references are retained
    with a diagnostic instead of being dropped."""
    tol = cfg["match-tolerance-ppm"]
    out = []
    for edge in doc.edges:
        a = _match_peak_id(edge["from_peak"], proton_peaks)
        b_pool = carbon_peaks if kind == KIND_HSQC else proton_peaks
        if kind == KIND_HMBC:
            b_pool = carbon_peaks
        b = _match_peak_id(edge["to_peak"], b_pool)
        e = dict(edge)
        e["resolved"] = bool(a and b)
        e["diagnostics"] = []
        if a is None:
            a = _nearest(proton_peaks, edge.get("shift1_ppm"), tol)
            if a is not None:
                e["from_peak_resolved"] = a["id"]
                e["resolved_by"] = "coordinate"
            else:
                e["diagnostics"].append("unresolved proton peak %r"
                                        % edge["from_peak"])
        else:
            e["from_peak_resolved"] = a["id"]
        if b is None:
            b = _nearest(b_pool, edge.get("shift2_ppm"), tol)
            if b is not None:
                e["to_peak_resolved"] = b["id"]
                e["resolved_by"] = "coordinate"
            else:
                e["diagnostics"].append("unresolved partner peak %r"
                                        % edge["to_peak"])
        else:
            e["to_peak_resolved"] = b["id"]
        e["resolved"] = bool(a and b)
        if e["resolved"]:
            e["from_peak_resolved"] = a["id"]
            e["to_peak_resolved"] = b["id"]
        out.append(e)
    return out


def _annotate_overlaps(peaks, cfg, findings, nucleus_label="1H"):
    """Mark peaks that coincide within match tolerance.

    Coincident peaks stay distinct records (no forced merging); the coupling
    network and scoring treat them as degenerate evidence.
    """
    tol = cfg["match-tolerance-ppm"]
    groups: List[List[Dict[str, Any]]] = []
    for p in sorted(peaks, key=lambda x: (x["shift_ppm"], x["id"])):
        placed = False
        for grp in groups:
            if abs(grp[0]["shift_ppm"] - p["shift_ppm"]) <= tol:
                grp.append(p)
                placed = True
                break
        if not placed:
            groups.append([p])
    for grp in groups:
        if len(grp) > 1:
            ids = sorted(p["id"] for p in grp)
            for p in grp:
                p["overlap_group"] = "ovl:" + ",".join(ids)
            findings.append(finding(
                FINDING_OVERLAP, SEV_WARN,
                "%s peaks %s coincide within %.3f ppm; no unique matching is "
                "forced" % (nucleus_label, ", ".join(ids), tol),
                affected=ids, shifts_ppm=[grp[0]["shift_ppm"]],
                tolerance_ppm=tol))
    return peaks


def _symmetry_findings(equiv, findings) -> None:
    for group in equiv:
        findings.append(finding(
            FINDING_SYMMETRY, SEV_INFO,
            "symmetry-equivalent atoms %s share resonance candidates; "
            "assignments stay grouped and are never arbitrarily split"
            % ", ".join(group["members"]),
            affected=list(group["members"]), group_id=group["id"]))


def _detect_bias(nuclei, peaks, nucleus_label, cfg, findings) -> float:
    """Median (observed - expected) residual over one-to-one nearest pairs."""
    if not peaks:
        return 0.0
    residuals = []
    for p in peaks:
        expected = [n["expected_ppm"] for n in nuclei
                    if n["expected_ppm"] is not None]
        if not expected:
            continue
        nearest = min(expected, key=lambda v: abs(p["shift_ppm"] - v))
        residuals.append(p["shift_ppm"] - nearest)
    if not residuals:
        return 0.0
    bias = float(np.median(residuals))
    threshold = cfg["bias-threshold-ppm"]
    if abs(bias) >= threshold:
        findings.append(finding(
            FINDING_BIAS, SEV_WARN,
            "global %s chemical-shift bias %.3f ppm (median residual); "
            "candidates scored with correction=%s"
            % (nucleus_label, bias, cfg["bias-correction"]),
            affected=[p["id"] for p in peaks], bias_ppm=bias,
            corrected=cfg["bias-correction"] == "on",
            threshold_ppm=threshold))
    return bias


def _carbon_of(nuc_id: str) -> str:
    return nuc_id.split("@")[-1] if "@" in nuc_id else nuc_id


def _expected_network(nuclei, adjacency):
    """Expected COSY/HSQC/HMBC support sets per nucleus.

    HSQC: H on carbon C <-> C.
    COSY: H on C <-> H on a carbon bonded to C (3J vicinal only).
    HMBC: H on C <-> carbon 2 or 3 bonds away through carbon skeleton.
    """
    by_id = {n["id"]: n for n in nuclei}
    carbons = {n["atom"]: n["id"] for n in nuclei if n["nucleus"] == "13C"}
    protons = [n for n in nuclei if n["nucleus"] == "1H"]
    h_by_carbon: Dict[str, List[str]] = {}
    for n in protons:
        h_by_carbon.setdefault(n["atom"], []).append(n["id"])
    expected: Dict[str, Dict[str, set]] = {n["id"]: {"cosy": set(),
                                                      "hsqc": set(),
                                                      "hmbc": set()}
                                           for n in nuclei}
    for n in protons:
        cid = n["atom"]
        if cid in carbons:
            expected[n["id"]]["hsqc"].add(carbons[cid])
            expected[carbons[cid]]["hsqc"].add(n["id"])
        for nbr in adjacency.get(cid, []):
            for hp in h_by_carbon.get(nbr, []):
                expected[n["id"]]["cosy"].add(hp)
        # HMBC: carbons two bonds (neighbour carbon) and three bonds away.
        for nbr in adjacency.get(cid, []):
            if nbr in carbons:
                expected[n["id"]]["hmbc"].add(carbons[nbr])
            for nbr2 in adjacency.get(nbr, []):
                if nbr2 != cid and nbr2 in carbons:
                    expected[n["id"]]["hmbc"].add(carbons[nbr2])
    return expected


def _expected_mult(nuc, nuclei) -> Optional[str]:
    """Predict first-order multiplicity from vicinal proton count.

    n+1 rule capped at octet; multiple vicinal groups give 'm'.
    """
    if nuc["nucleus"] != "1H":
        return None
    # Count protons on directly bonded carbons, excluding same carbon.
    same_carbon = [n["id"] for n in nuclei
                   if n["nucleus"] == "1H" and n["atom"] == nuc["atom"]]
    groups = {}
    for hp in nuc.get("proton_neighbours", []):
        if hp in same_carbon:
            continue
        cid = _carbon_of(hp)
        groups[cid] = groups.get(cid, 0) + 1
    if not groups:
        return "s"
    if len(groups) >= 2:
        return "m"
    count = next(iter(groups.values()))
    return {0: "s", 1: "d", 2: "t", 3: "q", 5: "sext", 6: "sept",
            7: "oct"}.get(count, "m")


def _mult_compatible(expected: Optional[str], observed: str) -> bool:
    if expected is None:
        return True
    if not observed:
        return False
    if observed in ("m", "br"):
        return True
    if observed == expected:
        return True
    return MULT_NEIGH.get(observed) == MULT_NEIGH.get(expected)


def _integral_score(nuc, peak, nuclei) -> Tuple[float, Dict[str, Any]]:
    if nuc["nucleus"] != "1H":
        return 1.0, {"detail": "n/a for 13C"}
    observed = peak.get("integral_h")
    # Expected integral is one per H nucleus; coincident overlap peaks may
    # legitimately carry the summed integral of several nuclei.
    overlap = len((peak.get("overlap_group") or "").replace("ovl:", "").split(",")) \
        if peak.get("overlap_group") else 1
    expected_total = max(1.0, float(overlap))
    if observed is None:
        return 0.5, {"detail": "no integral reported", "expected_h": 1.0}
    # The peak's integral is compatible with this nucleus if it covers >=1H
    # and does not exceed the total expected from the overlap group by >0.6H.
    detail = {"observed_h": observed, "expected_h": 1.0,
              "overlap_expected_h": expected_total}
    if observed + 0.5 < 1.0:
        return 0.0, detail
    if observed > expected_total + 0.6:
        return 0.25, detail
    return 1.0, detail


def _network_score(nuc, peak, nuclei_by_peak, edge_sets,
                   expected_edges) -> Tuple[float, Dict[str, Any]]:
    """Evidence-only coupling support in [0, 1].

    Evaluated under the hypothesis that ``nuc`` occupies ``peak``; the peak
    mapping is an evidence lens, never a forced assignment.  Missing support
    only weakens the score; extra observed edges are recorded.
    """
    detail = {"supporting": [], "missing": [], "extra": []}
    weights = {"cosy": 0.4, "hsqc": 0.4, "hmbc": 0.2}
    total = 0.0
    weight_sum = 0.0
    for kind, w in weights.items():
        exp = expected_edges.get(nuc["id"], {}).get(kind, set())
        if not exp:
            continue
        weight_sum += w
        supported = 0
        for edge in edge_sets.get(kind, []):
            if not edge["resolved"]:
                continue
            a, b = edge["from_peak_resolved"], edge["to_peak_resolved"]
            if peak["id"] not in (a, b):
                continue
            other_peak = b if a == peak["id"] else a
            other_nuc = nuclei_by_peak.get(other_peak)
            if other_nuc in exp:
                supported += 1
                detail["supporting"].append(
                    {"edge": edge["id"], "kind": kind,
                     "nucleus": other_nuc})
            else:
                detail["extra"].append(
                    {"edge": edge["id"], "kind": kind,
                     "peak": other_peak})
        miss = len(exp) - supported
        if miss > 0:
            detail["missing"].extend(
                {"kind": kind, "nucleus": e} for e in list(exp)[:miss])
        total += w * (supported / len(exp))
    score = total / weight_sum if weight_sum else 0.5
    return score, detail


def _greedy_peak_map(nuclei, proton_peaks, carbon_peaks, focus_nuc=None,
                     focus_peak=None) -> Dict[str, str]:
    """Nearest-expected-shift mapping used *only* to interpret edges.

    This is an evidence lens, not an assignment: overlaps stay one-to-many,
    and the focus candidate overrides the mapping so each candidate is
    evaluated under its own hypothesis.
    """
    mapping: Dict[str, str] = {}

    def assign(pool, nucs):
        order = sorted(pool, key=lambda p: p["id"])
        remaining = list(nucs)
        for p in order:
            usable = [n for n in remaining if n["expected_ppm"] is not None]
            if not usable:
                break
            best = min(usable, key=lambda n: abs(p["shift_ppm"]
                                                 - n["expected_ppm"]))
            mapping[p["id"]] = best["id"]
            # do not remove: overlapping peaks may support several nuclei
    assign(proton_peaks, [n for n in nuclei if n["nucleus"] == "1H"])
    assign(carbon_peaks, [n for n in nuclei if n["nucleus"] == "13C"])
    if focus_nuc is not None and focus_peak is not None:
        mapping[focus_peak["id"]] = focus_nuc
    return mapping


def _candidates_for(nuc, pools, nuclei, edge_sets, expected_edges, cfg,
                    bias) -> List[Dict[str, Any]]:
    expected = nuc["expected_ppm"]
    tol = cfg["proton-tolerance-ppm"] if nuc["nucleus"] == "1H" \
        else cfg["carbon-tolerance-ppm"]
    hard = cfg["proton-hard-tolerance-ppm"] if nuc["nucleus"] == "1H" \
        else cfg["carbon-hard-tolerance-ppm"]
    corrected_bias = bias if cfg["bias-correction"] == "on" else 0.0
    out: List[Dict[str, Any]] = []
    pool = pools[nuc["nucleus"]]
    if not pool:
        return out
    for peak in pool:
        reasons: List[str] = []
        cid = stable_id("cand", nuc["id"], peak["id"])
        if expected is None:
            out.append(_candidate(cid, nuc, peak, 0.0, CAND_UNKNOWN,
                                  ["no expected shift in rule pack"], {},
                                  cfg))
            continue
        residual = peak["shift_ppm"] - expected
        eff_residual = residual - corrected_bias
        shift_score = max(0.0, 1.0 - abs(eff_residual) / tol)
        if abs(eff_residual) > hard:
            reasons.append(
                "shift residual %.3f ppm beyond hard tolerance %.3f ppm"
                % (eff_residual, hard))
        mult_score = 1.0
        mult_detail: Dict[str, Any] = {}
        if nuc["nucleus"] == "1H":
            exp_mult = _expected_mult(nuc, nuclei)
            mult_detail = {"expected": exp_mult,
                           "observed": peak.get("mult_raw", "")}
            if peak.get("mult_raw"):
                if _mult_compatible(exp_mult, peak.get("mult", "")):
                    mult_score = 1.0
                else:
                    mult_score = 0.0
                    reasons.append("multiplicity %s incompatible with %s"
                                   % (peak.get("mult_raw"), exp_mult))
            else:
                mult_score = 0.5
        int_score, int_detail = _integral_score(nuc, peak, nuclei)
        if int_score == 0.0:
            reasons.append("integral %s does not cover one proton"
                           % peak.get("integral_raw"))
        hypothesis_map = _greedy_peak_map(nuclei, pools["1H"], pools["13C"],
                                          focus_nuc=nuc["id"],
                                          focus_peak=peak)
        net_score, net_detail = _network_score(
            nuc, peak, hypothesis_map, edge_sets, expected_edges)
        total = (cfg["weight-shift"] * shift_score
                 + cfg["weight-mult"] * mult_score
                 + cfg["weight-integral"] * int_score
                 + cfg["weight-network"] * net_score)
        components = {
            "shift": {"score": round(shift_score, 6),
                      "residual_ppm": round(residual, 6),
                      "corrected_residual_ppm": round(eff_residual, 6),
                      "bias_applied_ppm": round(corrected_bias, 6)},
            "multiplicity": {"score": round(mult_score, 6), **mult_detail},
            "integral": {"score": round(int_score, 6), **int_detail},
            "network": {"score": round(net_score, 6), **net_detail},
        }
        status = CAND_REJECTED if reasons else CAND_VIABLE
        out.append(_candidate(cid, nuc, peak, total, status, reasons,
                              components, cfg))
    out.sort(key=lambda c: (-c["score"], c["peak_id"]))
    return out


def _integral_score(nuc, peak, nuclei) -> Tuple[float, Dict[str, Any]]:
    if nuc["nucleus"] != "1H":
        return 1.0, {"detail": "n/a for 13C"}
    observed = peak.get("integral_h")
    # Expected integral is one per H nucleus; coincident overlap peaks may
    # legitimately carry the summed integral of several nuclei.
    overlap = len((peak.get("overlap_group") or "").replace("ovl:", "").split(",")) \
        if peak.get("overlap_group") else 1
    expected_total = max(1.0, float(overlap))
    if observed is None:
        return 0.5, {"detail": "no integral reported", "expected_h": 1.0}
    # The peak's integral is compatible with this nucleus if it covers >=1H
    # and does not exceed the total expected from the overlap group by >0.6H.
    detail = {"observed_h": observed, "expected_h": 1.0,
              "overlap_expected_h": expected_total}
    if observed + 0.5 < 1.0:
        return 0.0, detail
    if observed > expected_total + 0.6:
        return 0.25, detail
    return 1.0, detail


def _network_score(nuc, peak, nuclei_by_peak_hypothesis, edge_sets,
                   expected_edges) -> Tuple[float, Dict[str, Any]]:
    """Evidence-only coupling support in [0, 1].

    The score assumes, hypothetically, that ``nuc`` is assigned to ``peak``.
    Other nuclei' peak identities are provided by the caller; the network is
    never used to force a mapping -- missing support only weakens evidence.
    """
    detail = {"supporting": [], "missing": [], "extra": []}
    weights = {"cosy": 0.4, "hsqc": 0.4, "hmbc": 0.2}
    total = 0.0
    weight_sum = 0.0
    for kind, w in weights.items():
        exp = expected_edges.get(nuc["id"], {}).get(kind, set())
        if not exp:
            continue
        weight_sum += w
        supported = 0
        for edge in edge_sets.get(kind, []):
            if not edge["resolved"]:
                continue
            a, b = edge["from_peak_resolved"], edge["to_peak_resolved"]
            if peak["id"] not in (a, b):
                continue
            other_peak = b if a == peak["id"] else a
            other_nuc = nuclei_by_peak_hypothesis.get(other_peak)
            if other_nuc in exp:
                supported += 1
                detail["supporting"].append(
                    {"edge": edge["id"], "kind": kind,
                     "nucleus": other_nuc})
            else:
                detail["extra"].append(
                    {"edge": edge["id"], "kind": kind,
                     "peak": other_peak})
        miss = len(exp) - supported
        if miss > 0:
            detail["missing"].extend(
                {"kind": kind, "nucleus": e}
                for e in list(exp)[:miss])
        total += w * (supported / len(exp))
    score = total / weight_sum if weight_sum else 0.5
    return score, detail


def _greedy_peak_map(nuclei, proton_peaks, carbon_peaks, focus_nuc=None,
                     focus_peak=None) -> Dict[str, str]:
    """Nearest-expected-shift mapping used *only* to interpret edges.

    This is an evidence lens, not an assignment: overlaps stay one-to-many,
    and the focus candidate overrides the mapping so each candidate is
    evaluated under its own hypothesis.
    """
    mapping: Dict[str, str] = {}

    def assign(pool, nucs):
        order = sorted(pool, key=lambda p: p["id"])
        remaining = list(nucs)
        for p in order:
            usable = [n for n in remaining if n["expected_ppm"] is not None]
            if not usable:
                break
            best = min(usable, key=lambda n: abs(p["shift_ppm"]
                                                 - n["expected_ppm"]))
            mapping[p["id"]] = best["id"]
            # do not remove: overlapping peaks may support several nuclei
    assign(proton_peaks, [n for n in nuclei if n["nucleus"] == "1H"])
    assign(carbon_peaks, [n for n in nuclei if n["nucleus"] == "13C"])
    if focus_nuc is not None and focus_peak is not None:
        mapping[focus_peak["id"]] = focus_nuc
    return mapping


def _candidates_for(nuc, pool, nuclei, edge_sets, expected_edges, cfg,
                    bias) -> List[Dict[str, Any]]:
    expected = nuc["expected_ppm"]
    tol = cfg["proton-tolerance-ppm"] if nuc["nucleus"] == "1H" \
        else cfg["carbon-tolerance-ppm"]
    hard = cfg["proton-hard-tolerance-ppm"] if nuc["nucleus"] == "1H" \
        else cfg["carbon-hard-tolerance-ppm"]
    corrected_bias = bias if cfg["bias-correction"] == "on" else 0.0
    out: List[Dict[str, Any]] = []
    if not pool:
        return out
    for peak in pool:
        reasons: List[str] = []
        cid = stable_id("cand", nuc["id"], peak["id"])
        if expected is None:
            out.append(_candidate(cid, nuc, peak, 0.0, CAND_UNKNOWN,
                                  ["no expected shift in rule pack"], {},
                                  cfg))
            continue
        residual = peak["shift_ppm"] - expected
        eff_residual = residual - corrected_bias
        shift_score = max(0.0, 1.0 - abs(eff_residual) / tol)
        if abs(eff_residual) > hard:
            reasons.append(
                "shift residual %.3f ppm beyond hard tolerance %.3f ppm"
                % (eff_residual, hard))
        mult_score = 1.0
        mult_detail: Dict[str, Any] = {}
        if nuc["nucleus"] == "1H":
            exp_mult = _expected_mult(nuc, nuclei)
            mult_detail = {"expected": exp_mult,
                           "observed": peak.get("mult_raw", "")}
            if peak.get("mult_raw"):
                if _mult_compatible(exp_mult, peak.get("mult", "")):
                    mult_score = 1.0
                else:
                    mult_score = 0.0
                    reasons.append("multiplicity %s incompatible with %s"
                                   % (peak.get("mult_raw"), exp_mult))
            else:
                mult_score = 0.5
        proton_peaks = [p for p in nuclei]  # unused, replaced below
        int_score, int_detail = _integral_score(nuc, peak, nuclei)
        if int_score == 0.0:
            reasons.append("integral %.s does not cover one proton"
                           % peak.get("integral_raw"))


def _candidate(cid, nuc, peak, score, status, reasons, components, cfg
               ) -> Dict[str, Any]:
    return {
        "id": cid,
        "nucleus_id": nuc["id"],
        "nucleus_label": nuc["label"],
        "nucleus": nuc["nucleus"],
        "atom": nuc["atom"],
        "equiv_group": nuc.get("equiv_group"),
        "peak_id": peak["id"],
        "peak_shift_ppm": peak["shift_ppm"],
        "peak_shift_raw": peak.get("shift_raw"),
        "score": round(float(score), 6),
        "status": status,
        "reject_reasons": reasons,
        "components": components,
        "tie": False,
        "engine_version": ENGINE_VERSION,
        "rulepack_fingerprint": cfg["fingerprint"],
    }


def _missing_edge_findings(per_nucleus, edge_sets, expected_edges, nuclei,
                           findings) -> None:
    """An expected edge that has no observed counterpart for every viable
    candidate is weak negative evidence, never a rejection."""
    top = {nid: next((c for c in cands if c["status"] == CAND_VIABLE), None)
           for nid, cands in per_nucleus.items()}
    for nid, cand in top.items():
        if cand is None:
            continue
        comp = cand["components"].get("network", {})
        for miss in comp.get("missing", []):
            findings.append(finding(
                FINDING_MISSING_EDGE, SEV_WARN,
                "expected %s edge from %s to %s absent; treated as weak "
                "negative evidence only"
                % (miss["kind"], nid, miss["nucleus"]),
                affected=[nid, miss["nucleus"]], kind=miss["kind"]))


def _unknown_nuclei(nuclei, per_nucleus, candidates, findings) -> None:
    viable_peaks = set()
    for nuc in nuclei:
        for cand in per_nucleus.get(nuc["id"], []):
            if cand["status"] in (CAND_VIABLE, CAND_UNKNOWN):
                viable_peaks.add(cand["peak_id"])
    for nuc in nuclei:
        cands = per_nucleus.get(nuc["id"], [])
        if not cands or all(c["status"] == CAND_REJECTED for c in cands):
            uid = stable_id("cand", nuc["id"], "unknown")
            candidates.append({
                "id": uid,
                "nucleus_id": nuc["id"],
                "nucleus_label": nuc["label"],
                "nucleus": nuc["nucleus"],
                "atom": nuc["atom"],
                "equiv_group": nuc.get("equiv_group"),
                "peak_id": None,
                "peak_shift_ppm": None,
                "peak_shift_raw": None,
                "score": 0.0,
                "status": CAND_UNKNOWN,
                "reject_reasons": ["no viable observed peak; missing or "
                                   "overlapped resonance is not force-matched"],
                "components": {},
                "tie": False,
                "engine_version": ENGINE_VERSION,
                "rulepack_fingerprint": "",
            })
            findings.append(finding(
                FINDING_MISSING_PEAK, SEV_WARN,
                "%s nucleus %s has no viable peak; legal unknown state kept"
                % (nuc["nucleus"], nuc["id"]),
                affected=[nuc["id"]]))


def _tie_findings(nuclei, per_nucleus, cfg, findings) -> None:
    """Mark equally optimal legal candidates.

    Two kinds are reported:
      * within a nucleus, two viable peaks score within tie epsilon;
      * symmetry-equivalent nuclei sharing a peak (legally grouped, not split).
    """
    eps = cfg.get("tie-epsilon", 0.005)
    for nuc in nuclei:
        cands = [c for c in per_nucleus.get(nuc["id"], [])
                 if c["status"] == CAND_VIABLE]
        if len(cands) < 2:
            continue
        top_score = cands[0]["score"]
        tied = [c for c in cands if abs(c["score"] - top_score) <= eps]
        if len(tied) > 1:
            for c in tied:
                c["tie"] = True
            findings.append(finding(
                FINDING_TIE, SEV_INFO,
                "%s: candidates %s are equally optimal; both are retained"
                % (nuc["id"], ", ".join(c["peak_id"] for c in tied)),
                affected=[nuc["id"]],
                peaks=[c["peak_id"] for c in tied],
                score=top_score))
    # symmetry groups sharing one observed peak
    groups: Dict[str, List[str]] = {}
    for nuc in nuclei:
        if nuc.get("equiv_group"):
            groups.setdefault(nuc["equiv_group"], []).append(nuc["id"])
    for gid, members in groups.items():
        top_peaks = set()
        for nid in members:
            cands = [c for c in per_nucleus.get(nid, [])
                     if c["status"] == CAND_VIABLE]
            if cands:
                top_peaks.add(cands[0]["peak_id"])
        if len(top_peaks) <= 1:
            for nid in members:
                cands = per_nucleus.get(nid, [])
                if cands and cands[0]["status"] == CAND_VIABLE:
                    cands[0]["tie"] = True
            findings.append(finding(
                FINDING_TIE, SEV_INFO,
                "symmetry group %s: %s share peak %s and remain a legal tie"
                % (gid, ", ".join(members),
                   ",".join(sorted(top_peaks)) if top_peaks else "?"),
                affected=members, peaks=sorted(top_peaks), group_id=gid,
                symmetry=True))



def _extra_edges(edge_sets, per_nucleus, nuclei):
    """Observed edges unresolved to a candidate pair stay as raw evidence."""
    out = []
    for kind in ("cosy", "hsqc", "hmbc"):
        for edge in edge_sets.get(kind, []):
            if not edge["resolved"]:
                out.append({"edge_id": edge["id"], "kind": kind,
                            "diagnostics": edge.get("diagnostics", [])})
    return out
