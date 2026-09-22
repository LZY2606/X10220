"""Assignment candidate engine (algorithmic layer).

Design rules exercised by the acceptance suite:

* Overlapping peaks and missing peaks are never forced into a unique 1:1
  match.  Explicit ``overlap_group`` peaks may match several sites; a site
  with no usable observed peak gets an ``unknown`` "missing peak" candidate.
* The coupling network (COSY/HSQC/HMBC) only *adds evidence*.  A missing
  expected edge cannot reject a candidate; it merely removes a bonus.
* Symmetry-equivalent sites legitimately tie on the same observed peak.
* A near-constant chemical-shift offset is estimated and reported; when bias
  correction is enabled the corrected residuals are used, but the raw offset
  and the correction are both recorded.
* Candidates, rejection reasons, residuals and unknown states are all
  first-class outputs.  Deterministic; no randomness.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from .models import (Candidate, CandidateRun, Edge, ExperimentMeta, Molecule,
                     Parameters, Peak, RejectReason, Site)

ENGINE_ALGORITHM_VERSION = "assign-1.0.0"

_TOL = {"1H": "shift_tolerance_ppm_h", "13C": "shift_tolerance_ppm_c"}


def _tol(params: Parameters, nucleus: str) -> float:
    return getattr(params, _TOL[nucleus])


def _mult_score(expected: Optional[str], observed_norm: Optional[str]) -> float:
    if expected is None:
        return 0.8
    if observed_norm is None:
        return 0.4
    if observed_norm == "m":
        return 0.8
    return 1.0 if observed_norm == expected else 0.0


def _integral_score(expected: Optional[float], peak: Peak,
                    equivalents: int = 1) -> Tuple[float, Optional[str]]:
    if expected is None:
        return 1.0, None
    expected = expected * max(1, equivalents)
    if peak.integration is None or peak.integration.value is None:
        return 0.5, "integration unconvertible or missing; neutral 0.5"
    obs = peak.integration.value
    if obs <= 0:
        return 0.0, "non-positive integration"
    ratio = min(obs, expected) / max(obs, expected)
    if abs(obs - expected) < 0.25:
        return 1.0, None
    return max(0.0, ratio), f"integration {obs:g} vs expected {expected:g}"


def _gaussian(residual: float, tol: float) -> float:
    return float(math.exp(-(residual / tol) ** 2))


def estimate_bias(sites: List[Site], peaks: List[Peak],
                  params: Parameters) -> Tuple[Dict[str, float], List[str]]:
    """Median nearest-match residual (observed - expected) per nucleus."""
    diagnostics: List[str] = []
    bias: Dict[str, float] = {}
    for nucleus in ("1H", "13C"):
        obs = [p.shift.value for p in peaks
               if p.nucleus == nucleus and p.shift.value is not None]
        ss = [s for s in sites if s.nucleus == nucleus]
        if not obs or not ss:
            bias[nucleus] = 0.0
            continue
        deltas: List[float] = []
        tol = _tol(params, nucleus)
        for site in ss:
            nearest = min(obs, key=lambda v: abs(v - site.expected_shift))
            delta = nearest - site.expected_shift
            if abs(delta) <= 1.5 * tol:
                deltas.append(delta)
        if deltas:
            est = float(np.median(np.array(deltas)))
        else:
            est = 0.0
        bias[nucleus] = round(est, 4)
        if abs(est) > 0.45 * tol:
            diagnostics.append(
                f"{nucleus}: overall chemical-shift bias suspected "
                f"({est:+.3f} ppm vs skeleton reference)")
    return bias, diagnostics


def _build_expected_index(mol: Molecule) -> Dict[Tuple[str, str, str], List[str]]:
    """(kind, site_a, site_b) -> expected edge ids; symmetric lookup both ways."""
    idx: Dict[Tuple[str, str, str], List[str]] = {}
    for e in mol.expected_edges:
        for a, b in ((e.from_site, e.to_site), (e.to_site, e.from_site)):
            idx.setdefault((e.kind, a, b), []).append(e.id)
    return idx


def _correlation_component(site: Site, peak: Peak, kind: str,
                           observed: List[Edge], peaks_by_id: Dict[str, Peak],
                           sites_by_id: Dict[str, Site],
                           expected_idx, params: Parameters) -> Tuple[float, List[str]]:
    """Soft evidence from one correlation network.

    For every observed edge touching ``peak`` and every skeleton expectation
    touching ``site``, add the shift-match score of the counterpart pair.
    A missing edge simply yields 0 (no bonus); it never rejects anything.
    """
    best = 0.0
    used_edges: List[str] = []
    counterpart_nuc = {"COSY": site.nucleus, "HSQC": "13C" if site.nucleus == "1H" else "1H",
                       "HMBC": "13C" if site.nucleus == "1H" else "1H"}[kind]
    for edge in observed:
        if edge.kind != kind:
            continue
        other_peak_id = None
        if edge.from_peak == peak.id:
            other_peak_id = edge.to_peak
        elif edge.to_peak == peak.id:
            other_peak_id = edge.from_peak
        if other_peak_id is None:
            continue
        other_peak = peaks_by_id.get(other_peak_id)
        if other_peak is None or other_peak.nucleus != counterpart_nuc:
            continue
        for exp_id, other_sites in _expected_partners(site.id, kind, expected_idx):
            for other_site_id in other_sites:
                other_site = sites_by_id.get(other_site_id)
                if not other_site or other_site.nucleus != counterpart_nuc:
                    continue
                if other_peak.shift.value is None:
                    continue
                resid = abs(other_peak.shift.value - other_site.expected_shift)
                val = _gaussian(resid, _tol(params, counterpart_nuc))
                if val > best:
                    best = val
                    used_edges = [edge.id]
    return best, used_edges


def _expected_partners(site_id: str, kind: str, expected_idx):
    out = []
    for (k, a, b), ids in expected_idx.items():
        if k == kind and a == site_id:
            out.append((ids[0], [b]))
    return out


def generate_candidates(mol: Molecule, peaks: List[Peak], edges: List[Edge],
                        meta: ExperimentMeta, params: Parameters,
                        run_id: str) -> CandidateRun:
    peaks_by_id = {p.id: p for p in peaks}
    sites_by_id = {s.id: s for s in mol.sites}
    expected_idx = _build_expected_index(mol)
    observed = list(edges)

    bias, bias_diag = estimate_bias(mol.sites, peaks, params)
    diagnostics = list(bias_diag)

    # Sites sharing a symmetry group are chemically equivalent.
    sym_groups: Dict[str, List[str]] = {}
    for site in mol.sites:
        if site.symmetry_group:
            sym_groups.setdefault(site.symmetry_group, []).append(site.id)

    weights = {"multiplicity": params.multiplicity_weight,
               "integral": params.integral_weight,
               "hsqc": params.hsqc_weight, "cosy": params.cosy_weight,
               "hmbc": params.hmbc_weight}
    shift_weight = max(0.0, 1.0 - sum(weights.values()))

    candidates: List[Candidate] = []
    consumed_peaks: set = set()

    for site in mol.sites:
        site_cands: List[Candidate] = []
        same_nuc = [p for p in peaks
                    if p.nucleus == site.nucleus and p.shift.value is not None]
        correction = bias[site.nucleus] if params.bias_correction else 0.0
        for peak in same_nuc:
            corrected = peak.shift.value - correction
            resid = corrected - site.expected_shift
            absr = abs(resid)
            comps = {
                "shift": _gaussian(absr, _tol(params, site.nucleus)),
                "multiplicity": _mult_score(site.expected_mult,
                                            peak.multiplicity_norm)
                                if site.nucleus == "1H" else 1.0,
            }
            int_note = None
            if site.nucleus == "1H":
                comps["integral"], int_note = _integral_score(
                    site.expected_integral, peak,
                    equivalents=len(sym_groups.get(site.symmetry_group, [site.id])))
            else:
                comps["integral"] = 1.0
            comps["cosy"], e_cosy = _correlation_component(
                site, peak, "COSY", observed, peaks_by_id, sites_by_id,
                expected_idx, params)
            comps["hsqc"], e_hsqc = _correlation_component(
                site, peak, "HSQC", observed, peaks_by_id, sites_by_id,
                expected_idx, params)
            comps["hmbc"], e_hmbc = _correlation_component(
                site, peak, "HMBC", observed, peaks_by_id, sites_by_id,
                expected_idx, params)
            score = (shift_weight * comps["shift"]
                     + weights["multiplicity"] * comps["multiplicity"]
                     + weights["integral"] * comps["integral"]
                     + weights["cosy"] * comps["cosy"]
                     + weights["hsqc"] * comps["hsqc"]
                     + weights["hmbc"] * comps["hmbc"])
            score = round(score, 6)
            reasons: List[RejectReason] = []
            if absr > _tol(params, site.nucleus):
                reasons.append(RejectReason(
                    "SHIFT_OUT_OF_TOLERANCE",
                    f"|residual| {absr:.3f} ppm exceeds tolerance "
                    f"{_tol(params, site.nucleus):.3f} ppm",
                    evidence=[peak.id]))
            if site.nucleus == "1H" and comps["multiplicity"] == 0.0:
                reasons.append(RejectReason(
                    "MULTIPLICITY_MISMATCH",
                    f"observed {peak.multiplicity_norm!r} != expected "
                    f"{site.expected_mult!r}", evidence=[peak.id]))
            if int_note:
                reasons.append(RejectReason("INTEGRAL_MISMATCH", int_note,
                                            evidence=[peak.id]))
            site_cands.append(Candidate(
                id=f"cand-{site.id}-{peak.id}", site_id=site.id,
                nucleus=site.nucleus, peak_id=peak.id,
                status="legal" if (score >= params.min_score
                                   and absr <= _tol(params, site.nucleus)
                                   and (site.nucleus != "1H"
                                        or comps["multiplicity"] > 0.0))
                else "rejected",
                score=score, rank=0, residual_ppm=round(resid, 4),
                components={k: round(v, 4) for k, v in comps.items()},
                evidence_edges=sorted(set(e_cosy + e_hsqc + e_hmbc)),
                reasons=reasons, bias_applied_ppm=round(correction, 4),
                overlap=bool(peak.overlap_group),
                symmetry_ambiguous=site.symmetry_group in sym_groups
                                   and len(sym_groups[site.symmetry_group]) > 1))
        # Missing peak: never force a match onto another peak.
        usable = [c for c in site_cands
                  if c.status == "legal" and c.components["shift"] > 0.05]
        if not usable:
            site_cands.append(Candidate(
                id=f"cand-{site.id}-missing", site_id=site.id,
                nucleus=site.nucleus, peak_id=None, status="unknown", score=None,
                rank=0, residual_ppm=None,
                components={}, evidence_edges=[],
                reasons=[RejectReason(
                    "MISSING_PEAK",
                    "no observed peak within shift tolerance; "
                    "site left unassigned (coupling evidence cannot force it)")],
                symmetry_ambiguous=site.symmetry_group in sym_groups
                                   and len(sym_groups[site.symmetry_group]) > 1))
        site_cands.sort(key=lambda c: (c.score is None, -(c.score or 0.0)))
        for rank, cand in enumerate(site_cands, start=1):
            cand.rank = rank
            if (cand.peak_id and cand.status in ("legal", "tied")
                    and cand.components.get("shift", 0.0) > 0.05):
                consumed_peaks.add(cand.peak_id)
            candidates.append(cand)

    # Post-pass 1: within-site score ties among the best legal candidates.
    by_site: Dict[str, List[Candidate]] = {}
    for cand in candidates:
        by_site.setdefault(cand.site_id, []).append(cand)
    for site_id, cands in by_site.items():
        legal_like = [c for c in cands if c.status in ("legal", "tied")]
        if len(legal_like) >= 2:
            legal_like.sort(key=lambda c: -(c.score or 0.0))
            top, second = legal_like[0], legal_like[1]
            if (top.score is not None and second.score is not None
                    and abs(top.score - second.score) <= params.tie_epsilon
                    and (top.components.get("shift", 0.0) > 0.05
                         or second.components.get("shift", 0.0) > 0.05)):
                top.status = second.status = "tied"
                for tc in (top, second):
                    if not any(r.code == "TIED_BEST_CANDIDATE" for r in tc.reasons):
                        tc.reasons.append(RejectReason(
                            "TIED_BEST_CANDIDATE",
                            "scores are within tie epsilon; all legal candidates retained"))

    # Post-pass 2: symmetry-equivalent sites sharing one observed peak are an
    # explicit, chemically legitimate tie.
    for group, member_ids in sym_groups.items():
        if len(member_ids) < 2:
            continue
        members = [s for s in mol.sites if s.id in member_ids]
        for peak in peaks:
            if peak.nucleus != members[0].nucleus or peak.shift.value is None:
                continue
            correction = bias.get(members[0].nucleus, 0.0) if params.bias_correction else 0.0
            tol = _tol(params, members[0].nucleus)
            accepts = []
            for ms in members:
                if abs(peak.shift.value - correction - ms.expected_shift) <= tol:
                    accepts.append(ms.id)
            if len(accepts) < 2:
                continue
            for site_id in accepts:
                for cand in by_site[site_id]:
                    if cand.peak_id == peak.id and cand.status in ("legal", "tied"):
                        cand.symmetry_ambiguous = True
                        cand.status = "tied"
                        consumed_peaks.add(peak.id)
                        if not any(r.code == "SYMMETRY_EQUIVALENT"
                                   for r in cand.reasons):
                            cand.reasons.append(RejectReason(
                                "SYMMETRY_EQUIVALENT",
                                "equivalent sites cannot be distinguished by "
                                "chemical shift; all legal candidates retained",
                                evidence=[s for s in accepts if s != site_id]))

    for peak in peaks:
        if peak.shift.value is None:
            diagnostics.append(
                f"{peak.id}: shift unconvertible ({peak.shift.diagnostic}); "
                "kept in peak table, excluded from scoring")
        elif peak.id not in consumed_peaks:
            diagnostics.append(
                f"{peak.id}: no site accepts this peak above threshold "
                "(possible impurity / extra resonance)")

    return CandidateRun(
        id=run_id, params=params, param_fingerprint=params.fingerprint(),
        basis_revision="", candidates=candidates,
        bias_estimate=bias, diagnostics=diagnostics,
        algorithm_version=ENGINE_ALGORITHM_VERSION)


def _peer_best_shift(peer_site_id: str, peak_id: str, mol: Molecule,
                     peaks: List[Peak], params: Parameters,
                     bias: Dict[str, float]) -> bool:
    site = next(s for s in mol.sites if s.id == peer_site_id)
    peak = next((p for p in peaks if p.id == peak_id), None)
    if peak is None or peak.shift.value is None:
        return False
    corrected = peak.shift.value - bias.get(site.nucleus, 0.0)
    return abs(corrected - site.expected_shift) <= _tol(params, site.nucleus)
