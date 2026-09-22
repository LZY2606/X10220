import numpy as np

from .constants import (
    POLICY_OVERLAP_NO_FORCE,
    POLICY_SYMMETRY_EQUIVALENCE,
    POLICY_MISSING_EDGE_EVIDENCE_ONLY,
    POLICY_REFERENCE_BIAS_STALE,
    POLICY_TIED_CANDIDATES,
    POLICY_UNCONVERTED_KEEP_IN_PLACE,
)
from .utils import stable_id

TIE_EPS = 1e-6


def _conv_peaks(peak_sources):
    out = []
    diagnostics = []
    for src in peak_sources:
        for rec in src["records"]:
            if not rec.get("convertible"):
                diagnostics.append({
                    "kind": "unconverted_peak", "peak_id": rec["peak_id"],
                    "source_uid": src["uid"], "policy": POLICY_UNCONVERTED_KEEP_IN_PLACE,
                    "message": "peak kept at original position; excluded from matching",
                })
                continue
            out.append({
                "uid": rec["uid"], "peak_id": rec["peak_id"], "nucleus": rec["nucleus"],
                "shift_ppm": float(rec["shift_ppm"]),
                "intensity": rec.get("intensity"), "integration": rec.get("integration"),
                "multiplicity": rec.get("multiplicity_raw", ""),
                "source_uid": src["uid"], "source_version": src.get("version", 1),
            })
    return out, diagnostics


def _conv_atoms(molecule):
    atoms = []
    for rec in molecule["records"]:
        if rec.get("convertible") and rec.get("predicted_shift_ppm") is not None:
            atoms.append({
                "uid": rec["uid"], "atom_id": rec["atom_id"], "nucleus": rec["nucleus"],
                "element": rec.get("element", ""),
                "predicted_shift_ppm": float(rec["predicted_shift_ppm"]),
                "equivalence_group": rec.get("equivalence_group"),
                "coord": rec.get("coord"), "source_uid": molecule["uid"],
            })
        else:
            atoms.append({
                "uid": rec["uid"], "atom_id": rec["atom_id"], "nucleus": rec["nucleus"],
                "element": rec.get("element", ""), "predicted_shift_ppm": None,
                "equivalence_group": rec.get("equivalence_group"),
                "coord": rec.get("coord"), "source_uid": molecule["uid"], "unconverted": True,
            })
    return atoms


def _conv_edges(edge_sources):
    edges = []
    diagnostics = []
    for src in edge_sources:
        for rec in src["records"]:
            if not rec.get("convertible"):
                diagnostics.append({
                    "kind": "unconverted_edge", "edge_id": rec["edge_id"],
                    "source_uid": src["uid"], "policy": POLICY_UNCONVERTED_KEEP_IN_PLACE,
                    "message": "edge coordinate kept raw; excluded from matching",
                })
                continue
            edges.append({
                "uid": rec["uid"], "edge_id": rec["edge_id"], "kind": rec["kind"],
                "a_ppm": float(rec["a_ppm"]), "b_ppm": float(rec["b_ppm"]),
                "from_atom": rec.get("from_atom"), "to_atom": rec.get("to_atom"),
                "source_uid": src["uid"], "source_version": src.get("version", 1),
            })
    return edges, diagnostics


def overlap_groups(peaks, window):
    """Group consecutive observed peaks whose shifts fall within the overlap window."""
    ordered = sorted(peaks, key=lambda p: p["shift_ppm"])
    groups = []
    for peak in ordered:
        if groups and abs(peak["shift_ppm"] - groups[-1][-1]["shift_ppm"]) <= window:
            groups[-1].append(peak)
        else:
            groups.append([peak])
    return [g for g in groups if len(g) > 1]


def _edge_evidence(candidate, atoms_by_id, peaks_by_uid, edges, params):
    """Coupling network is supporting evidence only; a missing edge never rejects."""
    atom = candidate["atom"]
    peak = candidate["peak"]
    supports = []
    expected_checks = []

    def near(value, target, tol):
        return target is not None and abs(value - target) <= tol

    for edge in edges:
        kind = edge["kind"]
        if kind == "hsqc":
            h_tol, c_tol = params["hsqc_xy_tol_ppm"]
            if atom["nucleus"] == "1H":
                if near(edge["a_ppm"], peak["shift_ppm"], h_tol):
                    supports.append({"edge_id": edge["edge_id"], "kind": kind, "relation": "h-coordinate"})
            else:
                if near(edge["b_ppm"], peak["shift_ppm"], c_tol):
                    supports.append({"edge_id": edge["edge_id"], "kind": kind, "relation": "c-coordinate"})
        elif kind == "cosy" and atom["nucleus"] == "1H":
            if near(edge["a_ppm"], peak["shift_ppm"], params["cosy_tol_ppm"]) or near(
                edge["b_ppm"], peak["shift_ppm"], params["cosy_tol_ppm"]
            ):
                supports.append({"edge_id": edge["edge_id"], "kind": kind, "relation": "coupling-partner"})
        elif kind == "hmbc":
            h_tol, c_tol = params["hmbc_tol_ppm"]
            if atom["nucleus"] == "1H" and near(edge["a_ppm"], peak["shift_ppm"], h_tol):
                supports.append({"edge_id": edge["edge_id"], "kind": kind, "relation": "h-coordinate"})
            if atom["nucleus"] == "13C" and near(edge["b_ppm"], peak["shift_ppm"], c_tol):
                supports.append({"edge_id": edge["edge_id"], "kind": kind, "relation": "c-coordinate"})

    # Expected-but-unobserved edges: evidence only, never a hard rejection.
    for exp in candidate.get("_expected_edges", []):
        found = False
        for edge in edges:
            if edge["kind"] != exp["kind"]:
                continue
            if exp["kind"] == "cosy":
                if (near(edge["a_ppm"], exp["a_ppm"], params["cosy_tol_ppm"]) and
                        near(edge["b_ppm"], exp["b_ppm"], params["cosy_tol_ppm"])) or (
                        near(edge["b_ppm"], exp["a_ppm"], params["cosy_tol_ppm"]) and
                        near(edge["a_ppm"], exp["b_ppm"], params["cosy_tol_ppm"])):
                    found = True
                    break
            else:
                h_tol, c_tol = (params["hsqc_xy_tol_ppm"] if exp["kind"] == "hsqc"
                                else params["hmbc_tol_ppm"])
                if near(edge["a_ppm"], exp["a_ppm"], h_tol) and near(
                        edge["b_ppm"], exp["b_ppm"], c_tol):
                    found = True
                    break
        if not found:
            expected_checks.append({
                "kind": exp["kind"], "from_atom": exp.get("from_atom"),
                "to_atom": exp.get("to_atom"), "expected_h_ppm": exp.get("a_ppm"),
                "expected_c_ppm": exp.get("b_ppm"),
                "policy": POLICY_MISSING_EDGE_EVIDENCE_ONLY,
                "verdict": "missing_observed_edge_evidence_only",
            })
    return supports, expected_checks


def _expected_for_atom(atom_id, nucleus, atoms_by_id, expected_edges, observed_shifts_for_atom):
    out = []
    for exp in expected_edges:
        involved = atom_id in (exp.get("from_atom"), exp.get("to_atom"))
        if not involved:
            continue
        kind = exp["kind"]
        fa = atoms_by_id.get(exp.get("from_atom"))
        ta = atoms_by_id.get(exp.get("to_atom"))
        a_ppm = fa["predicted_shift_ppm"] if fa else None
        b_ppm = ta["predicted_shift_ppm"] if ta else None
        out.append({"kind": kind, "from_atom": exp.get("from_atom"),
                    "to_atom": exp.get("to_atom"), "a_ppm": a_ppm, "b_ppm": b_ppm})
    return out


def generate_run(run_uid, sources, params, rules_version):
    """Build candidate assignments + reject/unknown states from current source versions.

    All candidate ids are deterministic from (run_uid, atom_id, peak_id) so results
    are replayable and stable across export -> delete -> import.
    """
    np.random.seed(20260923)
    molecule = sources["molecule"]
    peak_sources = [sources["peaks_1h"], sources["peaks_13c"]]
    edge_sources = [sources[k] for k in ("cosy", "hsqc", "hmbc") if sources.get(k)]

    atoms = _conv_atoms(molecule)
    peaks, peak_diag = _conv_peaks(peak_sources)
    edges, edge_diag = _conv_edges(edge_sources)
    atoms_by_id = {a["atom_id"]: a for a in atoms}
    peaks_by_uid = {p["uid"]: p for p in peaks}
    expected_edges = molecule.get("expected_edges", [])

    peak_ov_groups = overlap_groups(peaks, params["overlap_window_h_ppm"])
    overlap_peak_ids = {p["peak_id"] for g in peak_ov_groups for p in g}

    candidates = []
    unknowns = []
    diagnostics = list(peak_diag) + list(edge_diag)

    for atom in atoms:
        if atom.get("unconverted") or atom["predicted_shift_ppm"] is None:
            unknowns.append({"atom_id": atom["atom_id"], "reason": "unconverted_predicted_shift",
                             "policy": POLICY_UNCONVERTED_KEEP_IN_PLACE})
            continue
        nucleus = atom["nucleus"]
        tol = params["shift_tolerance_h_ppm"] if nucleus == "1H" else params["shift_tolerance_c_ppm"]
        pool = [p for p in peaks if p["nucleus"] == nucleus]
        atom_candidates = []
        for peak in pool:
            error = abs(peak["shift_ppm"] - atom["predicted_shift_ppm"])
            if error > tol:
                continue
            expected = _expected_for_atom(
                atom["atom_id"], nucleus, atoms_by_id, expected_edges, None)
            provisional = {"atom": atom, "peak": peak, "_expected_edges": expected}
            supports, missing = _edge_evidence(
                provisional, atoms_by_id, peaks_by_uid, edges, params)
            shift_score = max(0.0, 1.0 - error / tol)
            edge_score = 1.0 if supports else 0.0
            score = (
                params["weight_shift"] * shift_score
                + params["weight_edge"] * edge_score
                - params["missing_edge_penalty"] * (1.0 if missing else 0.0)
            )
            score = round(float(score), 6)
            flags = []
            if peak["peak_id"] in overlap_peak_ids:
                flags.append(POLICY_OVERLAP_NO_FORCE)
            if atom.get("equivalence_group"):
                flags.append(POLICY_SYMMETRY_EQUIVALENCE)
            if missing:
                flags.append(POLICY_MISSING_EDGE_EVIDENCE_ONLY)
            cand = {
                "uid": stable_id("cand", (run_uid, atom["atom_id"], peak["peak_id"])),
                "atom_id": atom["atom_id"],
                "atom_uid": atom["uid"],
                "nucleus": nucleus,
                "peak_id": peak["peak_id"],
                "peak_uid": peak["uid"],
                "predicted_shift_ppm": atom["predicted_shift_ppm"],
                "observed_shift_ppm": peak["shift_ppm"],
                "shift_error_ppm": round(float(error), 6),
                "score": score,
                "shift_score": round(float(shift_score), 6),
                "edge_score": round(float(edge_score), 6),
                "supporting_edges": supports,
                "missing_expected_edges": missing,
                "flags": flags,
                "rank": None,
                "is_tied_best": False,
                "dependencies": {
                    "molecule_uid": molecule["uid"],
                    "peak_source_uid": peak["source_uid"],
                    "edge_source_uids": sorted({s["source_uid"] for s in supports}),
                    "param_fingerprint": params["__fingerprint__"],
                    "rules_version": rules_version,
                    "algorithm_version": "2026.09.0",
                },
            }
            atom_candidates.append(cand)
        if not atom_candidates:
            unknowns.append({"atom_id": atom["atom_id"], "reason": "no_peak_within_tolerance",
                             "predicted_shift_ppm": atom["predicted_shift_ppm"],
                             "nucleus": nucleus,
                             "policy": POLICY_OVERLAP_NO_FORCE})
            continue
        best = max(c["score"] for c in atom_candidates)
        for cand in atom_candidates:
            cand["is_tied_best"] = abs(cand["score"] - best) <= TIE_EPS
            if cand["is_tied_best"]:
                cand["flags"].append(POLICY_TIED_CANDIDATES)
        atom_candidates.sort(key=lambda c: (-c["score"], c["peak_id"]))
        for rank, cand in enumerate(atom_candidates, start=1):
            cand["rank"] = rank
        candidates.extend(atom_candidates)

    assigned_peak_ids = {c["peak_id"] for c in candidates}
    for peak in peaks:
        if peak["peak_id"] not in assigned_peak_ids:
            unknowns.append({"reason": "unassigned_peak", "peak_id": peak["peak_id"],
                             "nucleus": peak["nucleus"], "shift_ppm": peak["shift_ppm"],
                             "policy": POLICY_OVERLAP_NO_FORCE})

    # Overlap ambiguity: atoms whose tied-best targets sit inside one overlap group
    # form a joint ambiguity; nothing is forced to a unique match.
    ambiguities = []
    for group in peak_ov_groups:
        group_ids = {p["peak_id"] for p in group}
        involved = {}
        for cand in candidates:
            if cand["rank"] == 1 and cand["is_tied_best"] and cand["peak_id"] in group_ids:
                involved.setdefault(cand["atom_id"], []).append(cand["peak_id"])
        if len(involved) >= 1:
            ambiguities.append({
                "kind": "overlap_assignment_ambiguity",
                "policy": POLICY_OVERLAP_NO_FORCE,
                "peak_ids": sorted(group_ids),
                "atom_options": {a: sorted(v) for a, v in involved.items()},
                "verdict": "all_legal_candidates_retained_no_forced_unique_match",
            })

    # Global chemical-shift offset detection per nucleus (reference/solvent/temperature bias).
    biases = []
    for nucleus in ("1H", "13C"):
        errors = [c["shift_error_ppm"] * (1 if c["observed_shift_ppm"] >= c["predicted_shift_ppm"] else -1)
                  for c in candidates if c["nucleus"] == nucleus and c["rank"] == 1]
        best_pairs = [c for c in candidates if c["nucleus"] == nucleus and c["rank"] == 1]
        signed = [c["observed_shift_ppm"] - c["predicted_shift_ppm"] for c in best_pairs]
        if len(signed) >= 3:
            mean = round(float(np.mean(signed)), 6)
            std = round(float(np.std(signed)), 6)
            if abs(mean) >= params["bias_threshold_ppm"] and std <= params["bias_threshold_ppm"] * 1.5:
                biases.append({"nucleus": nucleus, "mean_offset_ppm": mean, "std_ppm": std,
                               "n_pairs": len(signed), "policy": POLICY_REFERENCE_BIAS_STALE,
                               "verdict": "global_offset_detected_dependent_candidates_stale"})
                for c in candidates:
                    if c["nucleus"] == nucleus:
                        c["flags"].append(POLICY_REFERENCE_BIAS_STALE)

    return {
        "run_uid": run_uid,
        "rules_version": rules_version,
        "algorithm_version": "2026.09.0",
        "param_fingerprint": params["__fingerprint__"],
        "params": {k: v for k, v in params.items() if k != "__fingerprint__"},
        "source_versions": sources["__versions__"],
        "candidates": candidates,
        "unknowns": unknowns,
        "ambiguities": ambiguities,
        "biases": biases,
        "overlap_groups": [{"nucleus": g[0]["nucleus"], "peak_ids": [p["peak_id"] for p in g]}
                           for g in peak_ov_groups],
        "diagnostics": diagnostics,
    }
