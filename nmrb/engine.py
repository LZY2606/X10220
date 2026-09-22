"""Assignment candidate generation engine.

For every assignment target (a single site or a symmetry equivalence
group) and every candidate peak the engine produces a first-class record
with chemical-shift score, coupling-evidence score, tie flags, rejection
reasons and "unknown / unassigned" status.  It never collapses
overlapping or symmetric peaks into a forced 1-to-1 match.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from . import ENGINE_VERSION
from .evidence import (
    direct_fingerprints,
    edge_support,
    expected_edge_lookup,
    overlap_groups,
)
from .ids import id_of

DEFAULT_PARAMS: Dict[str, Any] = {
    "sigma_ppm_1h": 0.25,
    "sigma_ppm_13c": 2.0,
    "edge_weight": 0.30,
    "max_delta_ppm_1h": 1.2,
    "max_delta_ppm_13c": 8.0,
    "overlap_tolerance_ppm_1h": 0.03,
    "overlap_tolerance_ppm_13c": 0.20,
}


def params_fingerprint(params: Dict[str, Any]) -> str:
    from .ids import fingerprint
    return fingerprint({"algorithm": ENGINE_VERSION, "params": params})


def _gaussian(delta: np.ndarray, sigma: float) -> np.ndarray:
    return np.exp(-0.5 * (delta / sigma) ** 2)


def _targets(facts: Dict[str, Any], nucleus: str) -> List[Dict[str, Any]]:
    sites = [s for s in facts["sites"] if s["nucleus"] == nucleus]
    members = facts["group_members"]
    sites_by_local = {s["local_id"]: s for s in sites}
    targets: List[Dict[str, Any]] = []
    grouped: set = set()
    for group in facts["groups"]:
        member_ids = members.get(group["group_id"], [])
        member_sites = [
            sites_by_local[m] for m in member_ids if m in sites_by_local
        ]
        if len(member_sites) != len(member_ids):
            continue
        grouped.update(member_ids)
        predicted = float(np.mean([s["predicted_ppm"] for s in member_sites]))
        labels = sorted(s["label"] or s["local_id"] for s in member_sites)
        targets.append(
            {
                "target_key": "grp:" + group["local_id"],
                "member_sites": [s["local_id"] for s in member_sites],
                "predicted_ppm": predicted,
                "label": "sym{" + ",".join(labels) + "}",
                "equivalent_n": len(member_sites),
            }
        )
    for site in sites:
        if site["local_id"] in grouped:
            continue
        targets.append(
            {
                "target_key": "site:" + site["local_id"],
                "member_sites": [site["local_id"]],
                "predicted_ppm": site["predicted_ppm"],
                "label": site["label"] or site["local_id"],
                "equivalent_n": 1,
            }
        )
    targets.sort(key=lambda t: t["target_key"])
    return targets


def generate(
    facts: Dict[str, Any],
    nucleus: str,
    params: Optional[Dict[str, Any]] = None,
    generation_event_id: str = "",
    generation_ordinal: int = 0,
    superseded: Optional[str] = None,
) -> Dict[str, Any]:
    params = dict(DEFAULT_PARAMS if params is None else params)
    from .ids import fingerprint
    pfp = fingerprint({"algorithm": ENGINE_VERSION, "params": params})
    peaks = [p for p in facts["peaks"] if p["nucleus"] == nucleus]
    sigma = params["sigma_ppm_1h" if nucleus == "1H" else "sigma_ppm_13c"]
    max_delta = params[
        "max_delta_ppm_1h" if nucleus == "1H" else "max_delta_ppm_13c"
    ]
    ovl_tol = params[
        "overlap_tolerance_ppm_1h"
        if nucleus == "1H"
        else "overlap_tolerance_ppm_13c"
    ]
    conditions = facts.get("conditions") or {}
    ref_offset = float(conditions.get("reference_offset_ppm") or 0.0)

    overlap_map, overlap_clusters = overlap_groups(peaks, ovl_tol)
    sites_by_local = {s["local_id"]: s for s in facts["sites"]}
    expected = expected_edge_lookup(facts["expected_edges"], sites_by_local)
    targets = _targets(facts, nucleus)
    shifts = np.array(
        [p["shift_ppm"] - ref_offset for p in peaks], dtype=float
    )

    candidates: List[Dict[str, Any]] = []
    for target in targets:
        predicted = target["predicted_ppm"]
        rows: List[Dict[str, Any]] = []
        for index, peak in enumerate(peaks):
            delta = float(shifts[index]) - predicted
            reject: List[Dict[str, Any]] = []
            if abs(delta) > max_delta:
                reject.append(
                    {
                        "code": "outside_tolerance",
                        "detail": "|delta|=%.3f ppm > %.3f ppm"
                        % (abs(delta), max_delta),
                    }
                )
            support, evidence, missing = edge_support(
                target["member_sites"],
                peak["peak_id"],
                facts["edges"],
                sites_by_local,
                expected,
                nucleus,
            )
            notes: List[Dict[str, Any]] = []
            if ref_offset:
                notes.append(
                    {
                        "code": "reference_offset_applied",
                        "detail": "corrected %.4f from raw %.4f (offset %+.4f)"
                        % (float(shifts[index]), peak["shift_ppm"], ref_offset),
                    }
                )
            if peak.get("intensity") is None:
                notes.append(
                    {
                        "code": "intensity_unconvertible",
                        "detail": "raw intensity %r retained"
                        % peak.get("intensity_raw"),
                    }
                )
            if peak.get("integral") is None:
                notes.append(
                    {
                        "code": "integral_unconvertible",
                        "detail": "raw integral %r retained"
                        % peak.get("integral_raw"),
                    }
                )
            rows.append(
                {
                    "peak": peak,
                    "delta": delta,
                    "support": support,
                    "evidence": evidence,
                    "missing": missing,
                    "reject": reject,
                    "notes": notes,
                }
            )
        deltas = np.array([r["delta"] for r in rows], dtype=float)
        shift_scores = _gaussian(deltas, sigma) if rows else np.array([])
        edge_weight = float(params["edge_weight"])
        scored = []
        for row, shift_score in zip(rows, shift_scores):
            if row["reject"]:
                total = -1.0
            else:
                total = (
                    (1.0 - edge_weight) * float(shift_score)
                    + edge_weight * float(row["support"])
                )
            scored.append((row, float(shift_score), total))
        order = sorted(
            range(len(scored)),
            key=lambda i: (
                scored[i][2] < 0,
                -scored[i][2],
                scored[i][0]["peak"]["peak_id"],
            ),
        )
        valid_totals = [t for _, _, t in scored if t >= 0]
        tied = len(valid_totals) > 1 and (
            max(valid_totals) - sorted(set(valid_totals))[-2]
            if len(set(valid_totals)) > 1
            else 0
        ) == 0 and max(valid_totals) > 0
        best_total = max(valid_totals, default=None)
        top_count = (
            sum(1 for t in valid_totals if abs(t - best_total) < 1e-12)
            if best_total is not None
            else 0
        )
        for rank, i in enumerate(order, start=1):
            row, shift_score, total = scored[i]
            peak = row["peak"]
            status = "rejected" if row["reject"] else "candidate"
            if (
                status == "candidate"
                and top_count > 1
                and abs(total - best_total) < 1e-12
            ):
                status = "tied"
            cand_identity = {
                "gen_event": generation_event_id,
                "target": target["target_key"],
                "peak": peak["peak_id"],
                "predicted": round(target["predicted_ppm"], 6),
                "algorithm": ENGINE_VERSION,
                "params_fp": pfp,
            }
            cand = {
                "candidate_id": id_of("cand", cand_identity),
                "target_key": target["target_key"],
                "target_nucleus": nucleus,
                "target_label": target["label"],
                "peak_id": peak["peak_id"],
                "shift_ppm": peak["shift_ppm"],
                "predicted_ppm": target["predicted_ppm"],
                "delta_ppm": round(row["delta"], 6),
                "shift_score": round(shift_score, 9),
                "edge_score": round(row["support"], 9),
                "total_score": round(total, 9),
                "rank": rank,
                "status": status,
                "multiplicity": peak.get("multiplicity", ""),
                "equivalent_n": target["equivalent_n"],
                "overlap_group": overlap_map.get(peak["peak_id"]),
                "evidence": row["evidence"],
                "reject": row["reject"],
                "notes": row["notes"]
                + [
                    {"code": "evidence_gap", "detail": m["detail"]}
                    for m in row["missing"]
                ],
                "ordinal": len(candidates),
            }
            if cand["overlap_group"]:
                cand["notes"].append(
                    {
                        "code": "overlapping_peak",
                        "detail": "overlap group %s; no forced unique match"
                        % cand["overlap_group"],
                    }
                )
            if target["equivalent_n"] > 1:
                cand["notes"].append(
                    {
                        "code": "symmetry_equivalent",
                        "detail": "equivalence group of %d sites"
                        % target["equivalent_n"],
                    }
                )
            candidates.append(cand)
        candidates.append(
            {
                "candidate_id": id_of(
                    "cand",
                    {
                        "gen_event": generation_event_id,
                        "target": target["target_key"],
                        "peak": None,
                        "algorithm": ENGINE_VERSION,
                        "params_fp": pfp,
                    },
                ),
                "target_key": target["target_key"],
                "target_nucleus": nucleus,
                "target_label": target["label"],
                "peak_id": None,
                "shift_ppm": None,
                "predicted_ppm": target["predicted_ppm"],
                "delta_ppm": None,
                "shift_score": None,
                "edge_score": None,
                "total_score": -0.01,
                "rank": len(order) + 1,
                "status": "unknown",
                "multiplicity": "",
                "equivalent_n": target["equivalent_n"],
                "overlap_group": None,
                "evidence": [],
                "reject": [],
                "notes": [
                    {
                        "code": "unassigned",
                        "detail": "no peak assigned to this target yet",
                    }
                ],
                "ordinal": len(candidates),
            }
        )

    direct = direct_fingerprints(facts["sources"])[nucleus]
    generation = {
        "generation_id": id_of(
            "gen",
            {
                "event": generation_event_id,
                "nucleus": nucleus,
                "params_fp": pfp,
                "direct": direct,
                "ordinal": generation_ordinal,
            },
        ),
        "algorithm": ENGINE_VERSION,
        "params": params,
        "params_fp": pfp,
        "scope": {"nucleus": nucleus},
        "direct": direct,
        "created_event": generation_event_id,
        "superseded_gen": superseded,
        "ordinal": generation_ordinal,
        "overlap_clusters": overlap_clusters,
        "candidates": candidates,
    }
    return generation
