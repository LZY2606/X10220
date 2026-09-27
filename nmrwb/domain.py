"""Domain layer: assignment candidate network with explainable evidence.

Candidates are never forced into a unique match. Overlapping peaks,
symmetric atoms and missing correlation edges stay first-class outcomes.
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from .db import next_id, next_seq, rows, row
from .events import emit
from .errors import DomainError
from .params import fingerprint

PEAK_KINDS = ("peaks_1h", "peaks_13c")
CORR_KINDS = ("cosy", "hsqc", "hmbc")
REL_TO_SOURCE = {"COSY": "cosy", "HSQC": "hsqc", "HMBC": "hmbc"}
NUCLEUS_ELEMENT = {"1H": "H", "13C": "C"}
COND_FIELDS = ["solvent", "temperature", "reference"]


def normalize_nucleus(raw: str) -> str | None:
    """Derived layer normalization; raw text stays on the peak row."""
    text = (raw or "").strip().upper().replace(" ", "")
    if text in ("1H", "H", "PROTON", "PROTONS"):
        return "1H"
    if text in ("13C", "C", "CARBON", "CARBONS"):
        return "13C"
    return None


def current_source_versions(conn, project_id) -> dict:
    found = rows(
        conn,
        "SELECT * FROM source_versions WHERE project_id=? AND superseded_seq IS NULL",
        (project_id,),
    )
    return {v["kind"]: v for v in found}


def current_conditions(conn, project_id):
    return row(
        conn,
        "SELECT * FROM conditions WHERE project_id=? AND superseded_seq IS NULL "
        "ORDER BY seq DESC LIMIT 1",
        (project_id,),
    )


def current_params(conn, project_id):
    return row(
        conn,
        "SELECT * FROM params_versions WHERE project_id=? AND superseded_seq IS NULL "
        "ORDER BY seq DESC LIMIT 1",
        (project_id,),
    )


def stale_candidates_with(conn, project_id, marker: str, seq: int) -> list[str]:
    staled = []
    for candidate in rows(
        conn,
        "SELECT id FROM candidates WHERE project_id=? AND stale_seq IS NULL "
        "AND deps LIKE ?",
        (project_id, f'%"{marker}"%'),
    ):
        conn.execute("UPDATE candidates SET stale_seq=? WHERE id=?", (seq, candidate["id"]))
        staled.append(candidate["id"])
    return staled


def _relation_kinds_by_nucleus(atoms, relations) -> dict:
    result = {}
    for nucleus, element in NUCLEUS_ELEMENT.items():
        element_atoms = {a["id"] for a in atoms if a["element"] == element}
        kinds = set()
        for rel in relations:
            if rel["kind"] == "BOND":
                continue
            if rel["a_atom_id"] in element_atoms or rel["b_atom_id"] in element_atoms:
                kinds.add(rel["kind"])
        result[nucleus] = kinds
    return result


def compute_run(conn, project_id, branch_id=None):
    versions = current_source_versions(conn, project_id)
    conds = current_conditions(conn, project_id)
    params_v = current_params(conn, project_id)
    if conds is None:
        raise DomainError("no acquisition conditions committed")
    if params_v is None:
        raise DomainError("no parameter set exists")
    params = json.loads(params_v["params"])
    atoms = rows(conn, "SELECT * FROM atoms WHERE project_id=? ORDER BY ord", (project_id,))
    if not atoms:
        raise DomainError("no molecular skeleton loaded")
    relations = rows(
        conn, "SELECT * FROM relations WHERE project_id=? ORDER BY ord", (project_id,)
    )
    if not any(kind in versions for kind in PEAK_KINDS):
        raise DomainError("no peak list committed yet")

    deps = {
        "sources": {kind: v["id"] for kind, v in sorted(versions.items())},
        "conditions": conds["id"],
        "params": params_v["id"],
    }

    peaks = []
    for kind in PEAK_KINDS:
        if kind not in versions:
            continue
        for peak in rows(
            conn,
            "SELECT * FROM peaks WHERE source_version_id=? ORDER BY ord",
            (versions[kind]["id"],),
        ):
            peak["nucleus"] = normalize_nucleus(peak["nucleus_raw"])
            peak["source_kind"] = kind
            peaks.append(peak)

    corrs = []
    for kind in CORR_KINDS:
        if kind in versions:
            corrs += rows(
                conn,
                "SELECT * FROM correlations WHERE source_version_id=? ORDER BY ord",
                (versions[kind]["id"],),
            )

    atom_by_id = {a["id"]: a for a in atoms}
    rel_index = defaultdict(set)
    for rel in relations:
        if rel["kind"] == "BOND":
            continue
        rel_index[(rel["kind"], rel["a_atom_id"])].add(rel["b_atom_id"])
        rel_index[(rel["kind"], rel["b_atom_id"])].add(rel["a_atom_id"])
    rel_kinds_by_nuc = _relation_kinds_by_nucleus(atoms, relations)

    latest = row(
        conn, "SELECT * FROM runs WHERE project_id=? ORDER BY seq DESC LIMIT 1", (project_id,)
    )
    if latest:
        old_deps = json.loads(latest["deps"])
        global_dirty = (
            old_deps["conditions"] != deps["conditions"]
            or old_deps["params"] != deps["params"]
        )

        def is_dirty(peak):
            if global_dirty:
                return True
            if old_deps["sources"].get(peak["source_kind"]) != deps["sources"].get(
                peak["source_kind"]
            ):
                return True
            nucleus = peak["nucleus"]
            if nucleus:
                for rel_kind in rel_kinds_by_nuc.get(nucleus, ()):
                    source_kind = REL_TO_SOURCE[rel_kind]
                    if old_deps["sources"].get(source_kind) != deps["sources"].get(
                        source_kind
                    ):
                        return True
            return False

        dirty = [p for p in peaks if is_dirty(p)]
    else:
        dirty = list(peaks)

    if latest and not dirty:
        emit(
            conn,
            project_id,
            branch_id,
            "run_computed",
            {"run_id": latest["id"], "noop": True, "reason": "dependencies unchanged"},
        )
        return latest["id"], {"noop": True, "run_id": latest["id"]}

    existing = rows(
        conn,
        "SELECT * FROM candidates WHERE project_id=? AND stale_seq IS NULL",
        (project_id,),
    )
    cand_atoms_by_peak = defaultdict(list)
    for candidate in existing:
        cand_atoms_by_peak[candidate["peak_id"]].append(candidate["atom_id"])

    new_cands = []
    unassigned = []
    for peak in dirty:
        if not peak["nucleus"]:
            unassigned.append(
                {"peak": peak["label"], "reason": "unknown_nucleus", "raw": peak["nucleus_raw"]}
            )
            continue
        if peak["shift_ppm"] is None:
            unassigned.append(
                {"peak": peak["label"], "reason": "unparseable_shift", "raw": peak["shift_raw"]}
            )
            continue
        element = NUCLEUS_ELEMENT[peak["nucleus"]]
        matched = False
        for atom in atoms:
            if atom["element"] != element:
                continue
            distance = abs(peak["shift_ppm"] - atom["expected_ppm"])
            if distance <= atom["window_ppm"]:
                matched = True
                new_cands.append(
                    {
                        "peak": peak,
                        "atom": atom,
                        "base": 1.0 - distance / atom["window_ppm"],
                        "bonus": 0.0,
                        "reasons": [],
                        "evidence": [],
                    }
                )
        if not matched:
            unassigned.append(
                {
                    "peak": peak["label"],
                    "reason": "no_atom_in_window",
                    "shift_ppm": peak["shift_ppm"],
                    "unit": "ppm",
                }
            )

    # register dirty-peak candidates so evidence can see dirty-to-dirty edges
    for nc in new_cands:
        cand_atoms_by_peak[nc["peak"]["id"]].append(nc["atom"]["id"])

    edges_by_peak = defaultdict(list)
    for edge in corrs:
        if edge["a_peak_id"] and edge["b_peak_id"]:
            edges_by_peak[edge["a_peak_id"]].append((edge, edge["b_peak_id"]))
            edges_by_peak[edge["b_peak_id"]].append((edge, edge["a_peak_id"]))

    bonus_table = params.get("evidence_bonus", {})
    for nc in new_cands:
        peak = nc["peak"]
        atom = nc["atom"]
        for edge, other_peak_id in edges_by_peak.get(peak["id"], []):
            targets = rel_index.get((edge["kind"], atom["id"]), set())
            if any(oid in targets for oid in cand_atoms_by_peak.get(other_peak_id, [])):
                nc["bonus"] += bonus_table.get(edge["kind"], 0.0)
                nc["evidence"].append(edge["id"])
        for kind in ("COSY", "HSQC", "HMBC"):
            for partner_id in sorted(rel_index.get((kind, atom["id"]), set())):
                found = False
                for edge, other_peak_id in edges_by_peak.get(peak["id"], []):
                    if edge["kind"] != kind:
                        continue
                    if partner_id in cand_atoms_by_peak.get(other_peak_id, []):
                        found = True
                        break
                if not found:
                    nc["reasons"].append(
                        f"missing_edge:{kind}:{atom_by_id[partner_id]['label']}"
                    )

    # overlap groups across all currently visible peaks of one nucleus
    peaks_by_nuc = defaultdict(list)
    for peak in peaks:
        if peak["nucleus"] and peak["shift_ppm"] is not None:
            peaks_by_nuc[peak["nucleus"]].append(peak)
    overlap_groups = {}
    peak_overlap = {}
    for nucleus, npeaks in peaks_by_nuc.items():
        ordered = sorted(npeaks, key=lambda p: (p["shift_ppm"], p["id"]))
        group = [ordered[0]] if ordered else []
        groups = []
        tol = params["overlap_ppm"].get(nucleus, 0.0)
        for prev, current in zip(ordered, ordered[1:]):
            if current["shift_ppm"] - prev["shift_ppm"] <= tol:
                group.append(current)
            else:
                groups.append(group)
                group = [current]
        if group:
            groups.append(group)
        for idx, members in enumerate(groups, start=1):
            if len(members) > 1:
                gid = f"ov-{nucleus}-{idx}"
                labels = [m["label"] for m in members]
                overlap_groups[gid] = {"nucleus": nucleus, "peaks": labels, "unit": "ppm"}
                for member in members:
                    peak_overlap[member["id"]] = gid

    for nc in new_cands:
        gid = peak_overlap.get(nc["peak"]["id"])
        if gid:
            nc["reasons"].append(f"overlap_group:{gid}")

    def score_of(nc):
        value = params["base_weight"] * nc["base"] + params["evidence_weight"] * min(
            1.0, nc["bonus"] / params["evidence_saturation"]
        )
        return round(value, int(params.get("score_round", 6)))

    new_by_peak = defaultdict(list)
    for nc in new_cands:
        new_by_peak[nc["peak"]["id"]].append(nc)
    for group_cands in new_by_peak.values():
        scores = {id(c): score_of(c) for c in group_cands}
        best_score = max(scores.values())
        for c in group_cands:
            if scores[id(c)] == best_score:
                peers = [
                    other
                    for other in group_cands
                    if other["atom"]["id"] != c["atom"]["id"] and scores[id(other)] == best_score
                ]
                if peers:
                    c["reasons"].append("tied_best")
            sym = c["atom"]["sym_group"]
            if sym:
                sym_peers = [
                    other
                    for other in group_cands
                    if other["atom"]["id"] != c["atom"]["id"]
                    and other["atom"]["sym_group"] == sym
                ]
                if sym_peers:
                    c["reasons"].append(f"symmetric_atoms:{sym}")

    run_seq = next_seq(conn)
    run_id = next_id(conn, "run")
    report = {
        "created_candidates": 0,
        "reused_current_candidates": len(existing),
        "dirty_peaks": [p["label"] for p in dirty],
        "unassigned": unassigned,
        "overlap_groups": overlap_groups,
        "bias": {},
        "units": {
            "shift": "ppm",
            "bias": "ppm",
            "score": "dimensionless [0,1]",
        },
    }
    for nc in new_cands:
        peak = nc["peak"]
        rel_kinds = rel_kinds_by_nuc.get(peak["nucleus"], set())
        source_deps = [versions[peak["source_kind"]]["id"]]
        for rel_kind in sorted(rel_kinds):
            source_kind = REL_TO_SOURCE[rel_kind]
            if source_kind in versions:
                source_deps.append(versions[source_kind]["id"])
        candidate_deps = {
            "sources": sorted(set(source_deps)),
            "conditions": conds["id"],
            "cond_fields": COND_FIELDS,
            "params": params_v["id"],
        }
        cid = next_id(conn, "cand")
        conn.execute(
            "INSERT INTO candidates(id,run_id,project_id,seq,peak_id,atom_id,score,"
            "reasons,deps,stale_seq) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                run_id,
                project_id,
                next_seq(conn),
                peak["id"],
                nc["atom"]["id"],
                score_of(nc),
                json.dumps(nc["reasons"], ensure_ascii=False),
                json.dumps(candidate_deps, sort_keys=True),
                None,
            ),
        )
        nc["id"] = cid
        report["created_candidates"] += 1

    # global shift bias diagnostic across all currently current candidates
    all_current = rows(
        conn,
        "SELECT c.*, p.shift_ppm AS shift_ppm, p.label AS peak_label, p.nucleus_raw "
        "FROM candidates c JOIN peaks p ON p.id=c.peak_id "
        "WHERE c.project_id=? AND c.stale_seq IS NULL",
        (project_id,),
    )
    offsets = defaultdict(list)
    used_peaks = defaultdict(list)
    by_peak_rows = defaultdict(list)
    for c in all_current:
        by_peak_rows[c["peak_id"]].append(c)
    for peak_id, group in by_peak_rows.items():
        best = max(g["score"] for g in group)
        winners = [g for g in group if g["score"] == best]
        if len(winners) != 1:
            continue  # tied peak cannot identify a unique offset
        winner = winners[0]
        nucleus = normalize_nucleus(winner["nucleus_raw"])
        atom = atom_by_id[winner["atom_id"]]
        offsets[nucleus].append(winner["shift_ppm"] - atom["expected_ppm"])
        used_peaks[nucleus].append(winner["peak_label"])
    bias_events = []
    for nucleus, values in offsets.items():
        median = float(np.median(np.array(values, dtype=float)))
        report["bias"][nucleus] = {
            "bias_ppm": round(median, 6),
            "peaks": used_peaks[nucleus],
        }
        threshold = params["bias_threshold_ppm"].get(nucleus, 0.0)
        if abs(median) > threshold:
            ev = emit(
                conn,
                project_id,
                branch_id,
                "shift_bias_detected",
                {
                    "nucleus": nucleus,
                    "bias_ppm": round(median, 6),
                    "threshold_ppm": threshold,
                    "unit": "ppm",
                    "peaks": used_peaks[nucleus],
                    "policy": "diagnostic only; raw shifts are not rewritten",
                },
            )
            bias_events.append(ev["id"])

    conn.execute(
        "INSERT INTO runs(id,project_id,branch_id,seq,algo_version,param_fp,deps,report,event_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            project_id,
            branch_id,
            run_seq,
            params.get("algo_version", "assign-1.0.0"),
            fingerprint(params),
            json.dumps(deps, sort_keys=True),
            json.dumps(report, ensure_ascii=False, sort_keys=True),
            None,
        ),
    )
    ev = emit(
        conn,
        project_id,
        branch_id,
        "run_computed",
        {
            "run_id": run_id,
            "noop": False,
            "algo_version": params.get("algo_version", "assign-1.0.0"),
            "param_fp": fingerprint(params),
            "deps": deps,
            "report": report,
            "bias_events": bias_events,
        },
    )
    conn.execute("UPDATE runs SET event_id=? WHERE id=?", (ev["id"], run_id))
    report["run_id"] = run_id
    return run_id, report
