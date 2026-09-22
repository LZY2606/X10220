"""NMR assignment engine: build reviewable candidate networks.

The engine never produces "one final number". For every nucleus it emits
candidates (viable / rejected / missing), per-component evidence, rejection
reasons, findings and dependency signatures. Missing correlation edges are
evidence only (never a rejection), overlapping peaks cannot be forced into a
unique match and tied optima are all retained.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .idgen import hash_id, fingerprint

MULT_FAMILY = {
    "s": "singlet", "d": "doublet", "dd": "doublet", "dt": "doublet",
    "t": "triplet", "td": "triplet", "q": "quartet", "sept": "septet",
    "septet": "septet", "m": "multiplet", "brs": "singlet", "?": "unknown",
}

FINDING_CODES = (
    "overlapping_peaks",
    "symmetric_equivalent_nuclei",
    "missing_correlation_edge",
    "global_shift_bias",
    "tied_optimal_candidates",
    "ambiguous_group_not_forced",
    "missing_peak",
    "unexpected_edge",
)


@dataclass
class Atom:
    atom_id: str
    element: str
    x: Optional[float]
    y: Optional[float]
    count: int = 1
    attach: Optional[str] = None
    expected_shift: Optional[dict] = None
    multiplicity: Optional[str] = None


def build_molecule(records: List[dict]) -> dict:
    atoms: Dict[str, Atom] = {}
    bonds: List[Tuple[str, str]] = []
    equivs: List[dict] = []
    for rec in records:
        if rec["type"] == "atom":
            aid = rec.get("atom_id")
            if not aid:
                continue
            atoms[aid] = Atom(
                atom_id=aid, element=rec.get("element"), x=rec.get("x"),
                y=rec.get("y"), count=rec.get("count", 1),
                attach=rec.get("attach"),
                expected_shift=rec.get("expected_shift"),
                multiplicity=rec.get("multiplicity"))
        elif rec["type"] == "bond":
            bonds.append((rec["a"], rec["b"]))
        elif rec["type"] == "equiv":
            equivs.append(rec)

    adjacency: Dict[str, set] = {}
    for a, b in bonds:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    h_sites, c_sites = _sites(atoms, equivs, adjacency)
    return {"atoms": atoms, "bonds": bonds, "adjacency": adjacency,
            "equivs": equivs, "h_sites": h_sites, "c_sites": c_sites}


def _sites(atoms, equivs, adjacency):
    h_members: Dict[str, List[str]] = {}
    for eq in equivs:
        h_members[eq["group_id"]] = list(eq["members"])
    grouped = {m for ms in h_members.values() for m in ms}

    h_sites: List[dict] = []
    for aid, atom in atoms.items():
        if atom.element != "H" or aid in grouped:
            continue
        h_sites.append(_h_site(aid, [aid], atom, adjacency, atoms, eq_group=None))
    for group_id, members in h_members.items():
        representative = next((m for m in members if m in atoms), members[0])
        atom = atoms.get(representative)
        count = sum(atoms[m].count for m in members if m in atoms) or len(members)
        h_sites.append(_h_site(group_id, members, atom, adjacency, atoms,
                               eq_group=group_id, count=count))
    h_sites.sort(key=lambda s: s["site_id"])

    c_sites = []
    for aid, atom in atoms.items():
        if atom.element == "C":
            expected = atom.expected_shift["value"] if atom.expected_shift else None
            attached_h = sorted(
                h for h, at in atoms.items()
                if at.element == "H" and at.attach == aid and h not in grouped)
            attached_h += sorted(
                gid for gid, members in h_members.items()
                if any(atoms.get(m) and atoms[m].attach == aid for m in members))
            c_sites.append({"site_id": aid, "nucleus": "13C",
                            "expected_shift_ppm": expected,
                            "attached_h_sites": sorted(set(attached_h))})
    c_sites.sort(key=lambda s: s["site_id"])
    return h_sites, c_sites


def _h_site(site_id, members, atom, adjacency, atoms, eq_group, count=None):
    attached_c = atom.attach if atom else None
    expected = atom.expected_shift["value"] if atom and atom.expected_shift else None
    mult = atom.multiplicity if atom else None
    return {
        "site_id": site_id, "nucleus": "1H", "member_atoms": list(members),
        "proton_count": atom.count if count is None and atom else (count or 1),
        "attached_carbon": attached_c, "expected_shift_ppm": expected,
        "expected_multiplicity": mult, "equiv_group": eq_group,
    }


def peaks_of(version_payload: Optional[dict]) -> List[dict]:
    if not version_payload:
        return []
    return [r for r in version_payload.get("records", []) if r["type"] == "peak"]


def edges_of(version_payload: Optional[dict]) -> List[dict]:
    if not version_payload:
        return []
    return [r for r in version_payload.get("records", []) if r["type"] == "edge"]


def run_assignment(
    *,
    run_id_seed: List[Any],
    algorithm_version: str,
    parameters: dict,
    molecule_payload: dict,
    h1d_payload: Optional[dict] = None,
    c13_payload: Optional[dict] = None,
    cosy_payload: Optional[dict] = None,
    hsqc_payload: Optional[dict] = None,
    hmbc_payload: Optional[dict] = None,
    source_versions: Optional[dict] = None,
) -> dict:
    rng = np.random.default_rng(parameters.get("seed", 20260923))
    mol = build_molecule(molecule_payload["records"])
    h_peaks = peaks_of(h1d_payload)
    c_peaks = peaks_of(c13_payload)
    cosy_edges = edges_of(cosy_payload)
    hsqc_edges = edges_of(hsqc_payload)
    hmbc_edges = edges_of(hmbc_payload)

    h_shifts = np.array([p["shift"]["value"] for p in h_peaks
                         if p["shift"]["value"] is not None], dtype=float)
    h_bias = _median_offset(mol["h_sites"], h_shifts) if h_shifts.size else 0.0
    c_shifts = np.array([p["shift"]["value"] for p in c_peaks
                         if p["shift"]["value"] is not None], dtype=float)
    c_bias = _median_offset(mol["c_sites"], c_shifts) if c_shifts.size else 0.0

    findings: List[dict] = []
    if abs(h_bias) >= parameters.get("bias_threshold_h", 0.05):
        findings.append(_finding("global_shift_bias",
                                 f"1H median(expected-observed) offset = {h_bias:.4f} ppm",
                                 axis="1H", bias_ppm=round(h_bias, 4)))
    if abs(c_bias) >= parameters.get("bias_threshold_c", 0.30):
        findings.append(_finding("global_shift_bias",
                                 f"13C median offset = {c_bias:.4f} ppm",
                                 axis="13C", bias_ppm=round(c_bias, 4)))

    for eq in mol["equivs"]:
        findings.append(_finding(
            "symmetric_equivalent_nuclei",
            f"equivalence group {eq['group_id']} shares one resonance "
            f"({', '.join(eq['members'])})",
            group_id=eq["group_id"], members=list(eq["members"]),
            record_id=eq["id"]))

    sites_out = []
    for site in mol["h_sites"]:
        sites_out.append(_evaluate_h_site(
            site, h_peaks, cosy_edges, hsqc_edges, hmbc_payload, h_bias,
            parameters, findings, source_versions or {}, mol))
    for site in mol["c_sites"]:
        sites_out.append(_evaluate_c_site(
            site, c_peaks, hsqc_edges, hmbc_edges, h_peaks, h_bias, c_bias,
            parameters, findings, source_versions or {}, mol))

    _edge_findings(mol, cosy_edges, hsqc_edges, hmbc_edges,
                   parameters, findings)
    joint = _joint_ties(sites_out, hsqc_edges, parameters)
    findings.extend(joint)

    for site in sites_out:
        viable = [c for c in site["candidates"] if c["status"] == "viable"]
        if viable and all(abs(c["score"] - viable[0]["score"]) < 1e-9
                          for c in viable) and len(viable) > 1:
            findings.append(_finding(
                "tied_optimal_candidates",
                f"{site['site_id']} has {len(viable)} equally best candidates; "
                f"all retained, none forced unique",
                site_id=site["site_id"],
                peak_record_ids=[c["peak_record_id"] for c in viable]))
        if site["status"] == "overlap_ambiguous":
            findings.append(_finding(
                "overlapping_peaks",
                f"peak {site['overlap_peak_record_id']} is a legal candidate for "
                f"multiple nuclei including {site['site_id']}; assignment not forced",
                site_id=site["site_id"],
                peak_record_id=site["overlap_peak_record_id"]))
        if site["status"] == "missing":
            findings.append(_finding(
                "missing_peak",
                f"no observed peak within window for {site['site_id']} "
                f"({site['nucleus']}); missing nucleus retained, not invented",
                site_id=site["site_id"], nucleus=site["nucleus"]))

    param_fp = fingerprint({"algorithm": algorithm_version, "parameters": parameters})
    run_id = hash_id("run", run_id_seed + [param_fp])
    return {
        "run_id": run_id,
        "algorithm_version": algorithm_version,
        "parameter_fingerprint": param_fp,
        "parameters": dict(parameters),
        "seed": parameters.get("seed", 20260923),
        "bias_h_ppm": round(h_bias, 4),
        "bias_c_ppm": round(c_bias, 4),
        "sites": sites_out,
        "findings": _dedupe_findings(findings),
        "source_versions": dict(source_versions or {}),
    }


def _median_offset(sites, observed_shifts):
    diffs = []
    for site in sites:
        expected = site.get("expected_shift_ppm")
        if expected is None:
            continue
        feasible = observed_shifts[np.abs(observed_shifts - expected) <= 0.6]
        if feasible.size:
            nearest = feasible[np.argmin(np.abs(feasible - expected))]
            diffs.append(expected - nearest)
    if not diffs:
        return 0.0
    return float(np.median(np.array(diffs)))


def _finding(code, message, **extra):
    finding = {"code": code, "message": message}
    finding.update(extra)
    return finding


def _dedupe_findings(findings):
    seen = set()
    out = []
    for f in findings:
        key = fingerprint(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
