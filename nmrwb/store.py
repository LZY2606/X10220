"""Project store: commits, decisions/branches, replay state, archive I/O."""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from .db import next_id, next_seq, rows, row
from .events import emit
from .errors import ConflictError, DomainError, FormatError, NotFoundError
from .importer import parse_peaks, parse_correlations, parse_num
from .params import DEFAULT_PARAMS, fingerprint, merge_params
from . import domain

SOURCE_KINDS = ("peaks_1h", "peaks_13c", "cosy", "hsqc", "hmbc")
ARCHIVE_FORMAT = "nmrwb-archive/1"
UNITS = {
    "shift": "ppm",
    "intensity": "instrument a.u.",
    "integration": "proton-equivalents",
    "temperature": "K",
    "frequency": "MHz",
    "score": "dimensionless [0,1]",
}
EXPORT_TABLES = [
    "projects",
    "branches",
    "params_versions",
    "source_versions",
    "peaks",
    "correlations",
    "conditions",
    "atoms",
    "relations",
    "runs",
    "candidates",
    "decisions",
    "events",
]


def _project(conn, project_id):
    project = row(conn, "SELECT * FROM projects WHERE id=?", (project_id,))
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project


def list_projects(conn):
    return rows(conn, "SELECT * FROM projects ORDER BY seq")


def create_project(conn, name: str) -> str:
    seq = next_seq(conn)
    project_id = next_id(conn, "proj")
    branch_id = next_id(conn, "br")
    conn.execute(
        "INSERT INTO projects(id,name,seq,head_branch_id,meta) VALUES(?,?,?,?,?)",
        (project_id, name, seq, branch_id, "{}"),
    )
    conn.execute(
        "INSERT INTO branches(id,project_id,name,parent_id,seq,fork_event_seq,label)"
        " VALUES(?,?,?,?,?,?,?)",
        (branch_id, project_id, "main", None, seq, None, "主干"),
    )
    emit(conn, project_id, branch_id, "project_created", {"name": name})
    params_id = next_id(conn, "par")
    conn.execute(
        "INSERT INTO params_versions(id,project_id,parent_id,seq,params,event_id,superseded_seq)"
        " VALUES(?,?,?,?,?,?,?)",
        (params_id, project_id, None, next_seq(conn),
         json.dumps(DEFAULT_PARAMS, sort_keys=True), None, None),
    )
    return project_id


def commit_conditions(conn, project_id, solvent, temperature_raw, reference, freq_raw,
                      branch_id=None):
    project = _project(conn, project_id)
    branch_id = branch_id or project["head_branch_id"]
    seq = next_seq(conn)
    old = domain.current_conditions(conn, project_id)
    temperature_k, tdiag = parse_num(temperature_raw)
    freq_mhz, fdiag = parse_num(freq_raw)
    diagnostics = [d for d in (tdiag, fdiag) if d]
    conditions_id = next_id(conn, "cond")
    conn.execute(
        "INSERT INTO conditions(id,project_id,parent_id,seq,solvent,temperature_raw,"
        "temperature_k,reference,freq_mhz_raw,freq_mhz,diag,event_id,superseded_seq)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (conditions_id, project_id, old["id"] if old else None, seq, solvent,
         str(temperature_raw), temperature_k, reference, str(freq_raw), freq_mhz,
         "; ".join(diagnostics) if diagnostics else None, None, None),
    )
    changed = []
    if old:
        if old["solvent"] != solvent:
            changed.append("solvent")
        if old["temperature_k"] != temperature_k:
            changed.append("temperature")
        if old["reference"] != reference:
            changed.append("reference")
        if old["freq_mhz"] != freq_mhz:
            changed.append("freq_mhz")
        conn.execute(
            "UPDATE conditions SET superseded_seq=? WHERE id=?", (seq, old["id"])
        )
    relevant = set(changed) & {"solvent", "temperature", "reference"}
    staled = []
    if relevant:
        for candidate in rows(
            conn,
            "SELECT id,deps FROM candidates WHERE project_id=? AND stale_seq IS NULL",
            (project_id,),
        ):
            deps = json.loads(candidate["deps"])
            if deps.get("conditions") == old["id"] and set(
                deps.get("cond_fields", [])
            ) & relevant:
                conn.execute(
                    "UPDATE candidates SET stale_seq=? WHERE id=?", (seq, candidate["id"])
                )
                staled.append(candidate["id"])
    ev = emit(
        conn,
        project_id,
        branch_id,
        "conditions_updated",
        {
            "conditions_id": conditions_id,
            "parent_id": old["id"] if old else None,
            "changed_fields": changed,
            "staled_candidates": staled,
            "diagnostics": diagnostics,
            "solvent": solvent,
            "temperature_raw": str(temperature_raw),
            "temperature_k": temperature_k,
            "temperature_unit": "K",
            "reference": reference,
            "freq_mhz_raw": str(freq_raw),
            "freq_mhz": freq_mhz,
            "freq_unit": "MHz",
        },
    )
    conn.execute("UPDATE conditions SET event_id=? WHERE id=?", (ev["id"], conditions_id))
    return {
        "conditions_id": conditions_id,
        "changed_fields": changed,
        "staled_candidates": staled,
        "diagnostics": diagnostics,
    }


def commit_source(conn, project_id, kind, raw_text, meta=None, branch_id=None):
    if kind not in SOURCE_KINDS:
        raise FormatError(f"unknown source kind: {kind}")
    project = _project(conn, project_id)
    branch_id = branch_id or project["head_branch_id"]
    parsed = parse_peaks(raw_text) if kind.startswith("peaks_") else parse_correlations(raw_text)
    versions = domain.current_source_versions(conn, project_id)
    parent = versions.get(kind)
    seq = next_seq(conn)
    version_id = next_id(conn, "src")
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO source_versions(id,project_id,parent_id,kind,seq,raw_text,sha256,"
        "meta,event_id,superseded_seq) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (version_id, project_id, parent["id"] if parent else None, kind, seq, raw_text,
         digest, json.dumps(meta or {}, ensure_ascii=False), None, None),
    )
    diagnostics = []
    if kind.startswith("peaks_"):
        for ordn, peak in enumerate(parsed):
            peak_id = next_id(conn, "pk")
            conn.execute(
                "INSERT INTO peaks(id,source_version_id,ord,label,nucleus_raw,shift_raw,"
                "shift_ppm,intensity_raw,intensity,integration_raw,integration,"
                "multiplicity,diag) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (peak_id, version_id, ordn, peak["label"], peak["nucleus"], peak["shift_raw"],
                 peak["shift"], peak["intensity_raw"], peak["intensity"],
                 peak["integration_raw"], peak["integration"], peak["multiplicity"],
                 peak["diag"]),
            )
            if peak["diag"]:
                diagnostics.append({"peak": peak["label"], "diag": peak["diag"]})
    else:
        label_map = {}
        for pk in ("peaks_1h", "peaks_13c"):
            current = versions.get(pk)
            if current:
                for found in rows(
                    conn,
                    "SELECT id,label FROM peaks WHERE source_version_id=? ORDER BY ord",
                    (current["id"],),
                ):
                    label_map.setdefault(found["label"], found["id"])
        for ordn, edge in enumerate(parsed):
            edge_id = next_id(conn, "cor")
            a_peak = label_map.get(edge["a"])
            b_peak = label_map.get(edge["b"])
            diag = None
            missing = []
            if a_peak is None:
                missing.append(edge["a"])
            if b_peak is None:
                missing.append(edge["b"])
            if missing:
                diag = f"unresolved peak label(s): {' '.join(missing)}; raw labels kept"
                diagnostics.append({"edge": f"{edge['kind']}:{edge['a']}-{edge['b']}", "diag": diag})
            conn.execute(
                "INSERT INTO correlations(id,source_version_id,ord,kind,a_label,b_label,"
                "a_peak_id,b_peak_id,diag) VALUES(?,?,?,?,?,?,?,?,?)",
                (edge_id, version_id, ordn, edge["kind"], edge["a"], edge["b"],
                 a_peak, b_peak, diag),
            )
    staled = []
    if parent:
        conn.execute(
            "UPDATE source_versions SET superseded_seq=? WHERE id=?", (seq, parent["id"])
        )
        staled = domain.stale_candidates_with(conn, project_id, parent["id"], seq)
    ev = emit(
        conn,
        project_id,
        branch_id,
        "source_committed",
        {
            "kind": kind,
            "version_id": version_id,
            "parent_id": parent["id"] if parent else None,
            "sha256": digest,
            "rows": len(parsed),
            "diagnostics": diagnostics,
            "staled_candidates": staled,
            "immutable": True,
        },
    )
    conn.execute("UPDATE source_versions SET event_id=? WHERE id=?", (ev["id"], version_id))
    return {
        "version_id": version_id,
        "parent_id": parent["id"] if parent else None,
        "rows": len(parsed),
        "diagnostics": diagnostics,
        "staled_candidates": staled,
    }


def compute(conn, project_id, branch_id=None):
    _project(conn, project_id)
    run_id, report = domain.compute_run(conn, project_id, branch_id)
    return {"run_id": run_id, "report": report}


def _ancestors(branch_id, branches_by_id):
    seen = set()
    current = branch_id
    while current and current in branches_by_id and current not in seen:
        seen.add(current)
        current = branches_by_id[current]["parent_id"]
    return seen


def _related_branches(a, b, branches_by_id):
    return a in _ancestors(b, branches_by_id) or b in _ancestors(a, branches_by_id)


def _find_candidate(conn, candidate_id):
    candidate = row(conn, "SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if candidate is None:
        raise NotFoundError(f"candidate {candidate_id} not found")
    return candidate


def _candidate_by_labels(conn, project_id, peak_label, atom_label):
    found = row(
        conn,
        "SELECT c.* FROM candidates c JOIN peaks p ON p.id=c.peak_id "
        "JOIN atoms a ON a.id=c.atom_id WHERE c.project_id=? AND c.stale_seq IS NULL "
        "AND p.label=? AND a.label=? ORDER BY c.seq DESC LIMIT 1",
        (project_id, peak_label, atom_label),
    )
    if found is None:
        raise NotFoundError(f"current candidate {peak_label}->{atom_label} not found")
    return found


def decide(conn, project_id, action, editor, candidate_id=None, note="",
           base_branch=None, params_patch=None):
    project = _project(conn, project_id)
    if action not in ("confirm", "reject", "adjust_params"):
        raise DomainError(f"unknown action: {action}")
    parent_branch = base_branch or project["head_branch_id"]
    if row(conn, "SELECT id FROM branches WHERE id=? AND project_id=?",
           (parent_branch, project_id)) is None:
        raise NotFoundError(f"base branch {parent_branch} not found")

    seq = next_seq(conn)
    branch_id = next_id(conn, "br")
    label_map = {"confirm": "确认", "reject": "否定", "adjust_params": "参数修订"}
    conn.execute(
        "INSERT INTO branches(id,project_id,name,parent_id,seq,fork_event_seq,label)"
        " VALUES(?,?,?,?,?,?,?)",
        (branch_id, project_id, f"{action}:{editor}@{seq}", parent_branch, seq, None,
         f"{label_map.get(action, action)} by {editor}"),
    )
    emit(
        conn, project_id, branch_id, "branch_created",
        {"branch_id": branch_id, "parent_id": parent_branch, "reason": action,
         "editor": editor},
    )

    impact = {}
    candidate = None
    if action in ("confirm", "reject"):
        candidate = _find_candidate(conn, candidate_id)
        siblings = rows(
            conn,
            "SELECT c.id FROM candidates c WHERE c.peak_id=? AND c.id!=? "
            "AND c.stale_seq IS NULL ORDER BY c.seq",
            (candidate["peak_id"], candidate["id"]),
        )
        impact = {
            "peak_id": candidate["peak_id"],
            "other_candidates_remain": [s["id"] for s in siblings],
            "forced_unique": False,
            "policy": "overlapping/symmetric/ambiguous peaks keep every legal candidate",
        }
        decision_payload = {"impact": impact, "candidate_status_at_decision": (
            "stale" if candidate["stale_seq"] is not None else "current")}
    else:
        current = domain.current_params(conn, project_id)
        old_params = json.loads(current["params"])
        new_params = merge_params(old_params, params_patch or {})
        params_id = next_id(conn, "par")
        conn.execute(
            "INSERT INTO params_versions(id,project_id,parent_id,seq,params,event_id,"
            "superseded_seq) VALUES(?,?,?,?,?,?,?)",
            (params_id, project_id, current["id"], next_seq(conn),
             json.dumps(new_params, sort_keys=True), None, seq),
        )
        conn.execute(
            "UPDATE params_versions SET superseded_seq=? WHERE id=?", (seq, current["id"])
        )
        emit(
            conn, project_id, branch_id, "params_adjusted",
            {"old_params_id": current["id"], "params_patch": params_patch or {},
             "new_param_fp": fingerprint(new_params), "parent_seq": seq,
             "rule": "revision creates a new version with parent linkage; old runs stay replayable"},
        )
        staled = domain.stale_candidates_with(conn, project_id, current["id"], seq)
        run_id, run_report = domain.compute_run(conn, project_id, branch_id)
        decision_payload = {
            "params_patch": params_patch or {},
            "old_params_id": current["id"],
            "new_params_id": params_id,
            "new_param_fp": fingerprint(new_params),
            "staled_candidates": staled,
            "new_run_id": run_id,
            "run_report": run_report,
        }
        impact = {"staled_candidates": staled, "new_run_id": run_id}

    decision_id = next_id(conn, "dec")
    conn.execute(
        "INSERT INTO decisions(id,project_id,branch_id,seq,candidate_id,action,editor,"
        "note,payload,undone_seq,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, project_id, branch_id, next_seq(conn),
         candidate["id"] if candidate else None, action, editor, note,
         json.dumps(decision_payload, ensure_ascii=False, sort_keys=True), None, None),
    )
    conn.execute("UPDATE projects SET head_branch_id=? WHERE id=?", (branch_id, project_id))
    ev = emit(
        conn, project_id, branch_id, "decision_made",
        {"decision_id": decision_id, "action": action, "editor": editor,
         "candidate_id": candidate["id"] if candidate else None, "note": note,
         "impact": impact, "branch_id": branch_id},
    )
    conn.execute("UPDATE decisions SET event_id=? WHERE id=?", (ev["id"], decision_id))

    conflicts = []
    if candidate is not None:
        conflicts = _record_conflicts(
            conn, project_id, decision_id, candidate["id"], action, editor, branch_id
        )
    return {
        "decision_id": decision_id,
        "branch_id": branch_id,
        "impact": impact,
        "conflicts": conflicts,
    }


def _record_conflicts(conn, project_id, decision_id, candidate_id, action, editor, branch_id):
    branches_by_id = {
        b["id"]: b for b in rows(conn, "SELECT * FROM branches WHERE project_id=?", (project_id,))
    }
    conflicts = []
    for other in rows(
        conn,
        "SELECT * FROM decisions WHERE project_id=? AND candidate_id=? AND id!=? "
        "AND undone_seq IS NULL",
        (project_id, candidate_id, decision_id),
    ):
        if other["action"] == action or other["editor"] == editor:
            continue
        if _related_branches(other["branch_id"], branch_id, branches_by_id):
            continue
        payload = {
            "candidate_id": candidate_id,
            "decisions": [other["id"], decision_id],
            "editors": [other["editor"], editor],
            "actions": [other["action"], action],
            "branches": [other["branch_id"], branch_id],
            "rule": "sibling-branch decisions disagree; neither overwrites the other",
        }
        ev = emit(conn, project_id, branch_id, "conflict_detected", payload)
        conflicts.append({"event_id": ev["id"], **payload})
    return conflicts


def undo_decision(conn, project_id, decision_id):
    decision = row(
        conn, "SELECT * FROM decisions WHERE id=? AND project_id=?",
        (decision_id, project_id),
    )
    if decision is None:
        raise NotFoundError(f"decision {decision_id} not found")
    if decision["undone_seq"] is not None:
        raise DomainError("decision is already undone")
    seq = next_seq(conn)
    conn.execute("UPDATE decisions SET undone_seq=? WHERE id=?", (seq, decision_id))
    ev = emit(
        conn, project_id, decision["branch_id"], "decision_undone",
        {"decision_id": decision_id, "action": decision["action"],
         "editor": decision["editor"], "candidate_id": decision["candidate_id"]},
    )
    return {"event_id": ev["id"], "decision_id": decision_id}


def resolve_conflict(conn, project_id, keep_decision_id, drop_decision_id):
    keep = row(conn, "SELECT * FROM decisions WHERE id=? AND project_id=?",
               (keep_decision_id, project_id))
    drop = row(conn, "SELECT * FROM decisions WHERE id=? AND project_id=?",
               (drop_decision_id, project_id))
    if keep is None or drop is None:
        raise NotFoundError("one of the conflict decisions does not exist")
    if keep["candidate_id"] != drop["candidate_id"]:
        raise DomainError("conflict decisions must concern the same candidate")
    if drop["undone_seq"] is not None:
        raise DomainError("dropped decision is already undone")
    undo = undo_decision(conn, project_id, drop_decision_id)
    ev = emit(
        conn, project_id, keep["branch_id"], "conflict_resolved",
        {"keep_decision_id": keep_decision_id, "drop_decision_id": drop_decision_id,
         "candidate_id": keep["candidate_id"], "resolution": "drop undone; keep stands",
         "undo_event_id": undo["event_id"]},
    )
    return {"event_id": ev["id"], "kept": keep_decision_id, "dropped": drop_decision_id}


def _conflicts(decisions, branches_by_id):
    active = [d for d in decisions if not d["undone"] and d["candidate_id"]]
    result = []
    for i, first in enumerate(active):
        for second in active[i + 1:]:
            if first["candidate_id"] != second["candidate_id"]:
                continue
            if first["action"] == second["action"] or first["editor"] == second["editor"]:
                continue
            if _related_branches(first["branch_id"], second["branch_id"], branches_by_id):
                continue
            result.append({
                "candidate_id": first["candidate_id"],
                "decision_ids": [first["id"], second["id"]],
                "editors": [first["editor"], second["editor"]],
                "actions": [first["action"], second["action"]],
            })
    return result


def state(conn, project_id, branch_id=None, asof=None):
    project = _project(conn, project_id)
    latest = row(conn, "SELECT COALESCE(MAX(seq),0) AS s FROM events WHERE project_id=?",
                 (project_id,))
    latest_seq = latest["s"]
    cutoff = latest_seq if asof is None else min(int(asof), latest_seq)

    branches = [
        b for b in rows(conn, "SELECT * FROM branches WHERE project_id=? ORDER BY seq",
                       (project_id,))
        if b["seq"] <= cutoff
    ]
    branches_by_id = {b["id"]: b for b in branches}
    view_branch = branch_id if branch_id in branches_by_id else project["head_branch_id"]
    if view_branch not in branches_by_id and branches:
        view_branch = branches[0]["id"]
    ancestry = _ancestors(view_branch, branches_by_id)

    versions = [
        v for v in rows(
            conn, "SELECT * FROM source_versions WHERE project_id=? ORDER BY kind, seq",
            (project_id,))
        if v["seq"] <= cutoff and (v["superseded_seq"] is None or v["superseded_seq"] > cutoff)
    ]
    peaks = []
    correlations = []
    for version in versions:
        if version["kind"].startswith("peaks_"):
            for peak in rows(conn, "SELECT * FROM peaks WHERE source_version_id=? ORDER BY ord",
                             (version["id"],)):
                peak["source_kind"] = version["kind"]
                peak["source_version_id"] = version["id"]
                peak["nucleus_normalized"] = domain.normalize_nucleus(peak["nucleus_raw"])
                peaks.append(peak)
        else:
            for edge in rows(conn, "SELECT * FROM correlations WHERE source_version_id=? "
                             "ORDER BY ord", (version["id"],)):
                edge["source_kind"] = version["kind"]
                edge["source_version_id"] = version["id"]
                correlations.append(edge)

    conditions_row = None
    for candidate_cond in rows(conn, "SELECT * FROM conditions WHERE project_id=? ORDER BY seq",
                               (project_id,)):
        if candidate_cond["seq"] <= cutoff and (
            candidate_cond["superseded_seq"] is None
            or candidate_cond["superseded_seq"] > cutoff
        ):
            conditions_row = candidate_cond
    params_row = None
    for candidate_par in rows(conn, "SELECT params_versions.* FROM params_versions WHERE project_id=? "
                              "ORDER BY seq", (project_id,)):
        if candidate_par["seq"] <= cutoff and (
            candidate_par["superseded_seq"] is None
            or candidate_par["superseded_seq"] > cutoff
        ):
            params_row = candidate_par

    atoms = rows(conn, "SELECT * FROM atoms WHERE project_id=? ORDER BY ord", (project_id,))
    relations = rows(conn, "SELECT * FROM relations WHERE project_id=? ORDER BY ord",
                     (project_id,))
    peak_by_id = {p["id"]: p for p in peaks}
    atom_by_id = {a["id"]: a for a in atoms}

    candidates = []
    for candidate in rows(conn, "SELECT * FROM candidates WHERE project_id=? ORDER BY seq",
                          (project_id,)):
        if candidate["seq"] > cutoff:
            continue
        candidate["status"] = (
            "stale" if candidate["stale_seq"] is not None
            and candidate["stale_seq"] <= cutoff else "current"
        )
        candidate["reasons"] = json.loads(candidate["reasons"])
        candidate["deps"] = json.loads(candidate["deps"])
        peak = peak_by_id.get(candidate["peak_id"])
        atom = atom_by_id.get(candidate["atom_id"])
        candidate["peak_label"] = peak["label"] if peak else None
        candidate["atom_label"] = atom["label"] if atom else None
        candidate["nucleus"] = domain.normalize_nucleus(peak["nucleus_raw"]) if peak else None
        candidates.append(candidate)

    decisions = []
    for decision in rows(conn, "SELECT * FROM decisions WHERE project_id=? ORDER BY seq",
                         (project_id,)):
        if decision["seq"] > cutoff:
            continue
        decision["undone"] = (
            decision["undone_seq"] is not None and decision["undone_seq"] <= cutoff
        )
        decision["payload"] = json.loads(decision["payload"])
        decisions.append(decision)

    effective = {}
    for decision in sorted(decisions, key=lambda d: d["seq"]):
        if decision["branch_id"] in ancestry and not decision["undone"] and decision["candidate_id"]:
            effective[decision["candidate_id"]] = decision["id"]

    events = []
    for event in rows(conn, "SELECT * FROM events WHERE project_id=? ORDER BY seq",
                      (project_id,)):
        if event["seq"] <= cutoff:
            event["payload"] = json.loads(event["payload"])
            events.append(event)

    run_rows = []
    for run in rows(conn, "SELECT * FROM runs WHERE project_id=? ORDER BY seq", (project_id,)):
        if run["seq"] <= cutoff:
            run["deps"] = json.loads(run["deps"])
            run["report"] = json.loads(run["report"])
            run_rows.append(run)

    return {
        "project": {
            "id": project["id"],
            "name": project["name"],
            "head_branch_id": project["head_branch_id"],
            "meta": json.loads(project["meta"] or "{}"),
        },
        "view_branch_id": view_branch,
        "asof_seq": cutoff,
        "latest_seq": latest_seq,
        "historical": cutoff < latest_seq,
        "branches": branches,
        "units": UNITS,
        "source_versions": versions,
        "conditions": conditions_row,
        "params": {
            "id": params_row["id"],
            "params": json.loads(params_row["params"]),
            "fingerprint": fingerprint(json.loads(params_row["params"])),
        } if params_row else None,
        "peaks": peaks,
        "correlations": correlations,
        "atoms": atoms,
        "relations": relations,
        "candidates": candidates,
        "decisions": decisions,
        "effective_decisions": effective,
        "conflicts": _conflicts(decisions, branches_by_id),
        "events": events,
        "runs": run_rows,
    }


def export_project(conn, project_id):
    _project(conn, project_id)
    archive = {"format": ARCHIVE_FORMAT, "project_id": project_id, "tables": {}}
    for table in EXPORT_TABLES:
        if table in ("peaks", "correlations"):
            sql = (
                f"SELECT t.* FROM {table} t JOIN source_versions s "
                "ON s.id=t.source_version_id WHERE s.project_id=? "
                f"ORDER BY t.source_version_id, t.ord"
            )
        elif table in ("events", "branches", "source_versions", "conditions",
                       "params_versions", "runs", "decisions", "atoms",
                       "relations", "candidates"):
            sql = f"SELECT * FROM {table} WHERE project_id=? ORDER BY seq" if table != "atoms" \
                else f"SELECT * FROM {table} WHERE project_id=? ORDER BY ord"
            if table == "relations":
                sql = f"SELECT * FROM {table} WHERE project_id=? ORDER BY ord"
        else:
            sql = "SELECT * FROM projects WHERE id=?"
        archive["tables"][table] = rows(conn, sql, (project_id,))
    return archive


_ID_RE = re.compile(r"^([a-z]+)-(\d{6})$")
_SEQ_COLUMNS = ("seq", "superseded_seq", "stale_seq", "undone_seq", "fork_event_seq")


def import_project(conn, archive):
    if archive.get("format") != ARCHIVE_FORMAT:
        raise FormatError(f"unsupported archive format: {archive.get('format')}")
    tables = archive.get("tables", {})
    project_rows = tables.get("projects", [])
    if not project_rows:
        raise FormatError("archive has no project row")
    project_id = project_rows[0]["id"]
    if row(conn, "SELECT id FROM projects WHERE id=?", (project_id,)):
        raise ConflictError(f"project {project_id} already exists")
    for table in EXPORT_TABLES:
        for record in tables.get(table, []):
            columns = ",".join(record.keys())
            placeholders = ",".join("?" for _ in record)
            conn.execute(
                f"INSERT INTO {table}({columns}) VALUES({placeholders})",
                tuple(record.values()),
            )
    max_seq = 0
    prefixes = {}
    for table in EXPORT_TABLES:
        for record in tables.get(table, []):
            for key, value in record.items():
                if isinstance(value, str):
                    match = _ID_RE.match(value)
                    if match:
                        prefixes[match.group(1)] = max(
                            prefixes.get(match.group(1), 0), int(match.group(2))
                        )
                if key in _SEQ_COLUMNS and isinstance(value, int):
                    max_seq = max(max_seq, value)
    for prefix, value in prefixes.items():
        conn.execute(
            "INSERT INTO counters(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=MAX(excluded.value, counters.value)",
            (prefix, value),
        )
    conn.execute(
        "INSERT INTO counters(name,value) VALUES('seq',?) "
        "ON CONFLICT(name) DO UPDATE SET value=MAX(excluded.value, counters.value)",
        (max_seq,),
    )
    return project_id


def load_casepack(conn, project_id, pack_dir):
    pack_dir = Path(pack_dir)
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    project = _project(conn, project_id)
    emit(
        conn, project_id, project["head_branch_id"], "casepack_loaded",
        {"casepack": manifest["id"], "title": manifest["title"],
         "compound": manifest.get("compound")},
    )
    skeleton = json.loads((pack_dir / manifest["skeleton"]).read_text(encoding="utf-8"))
    conn.execute(
        "UPDATE projects SET meta=? WHERE id=?",
        (json.dumps({
            "casepack": manifest["id"],
            "title": manifest["title"],
            "compound": skeleton["compound"],
            "formula": skeleton["formula"],
            "coord_system": skeleton["coord_system"],
        }, ensure_ascii=False), project_id),
    )
    atom_ids = {}
    for ordn, atom in enumerate(skeleton["atoms"]):
        atom_id = next_id(conn, "atom")
        atom_ids[atom["label"]] = atom_id
        conn.execute(
            "INSERT INTO atoms(id,project_id,ord,label,element,expected_ppm,window_ppm,"
            "sym_group,x,y) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (atom_id, project_id, ordn, atom["label"], atom["element"],
             atom["expected_ppm"], atom["window_ppm"], atom.get("sym_group"),
             atom["x"], atom["y"]),
        )
    ordn = 0
    for bond in skeleton.get("bonds", []):
        rel_id = next_id(conn, "rel")
        conn.execute(
            "INSERT INTO relations(id,project_id,ord,kind,a_atom_id,b_atom_id)"
            " VALUES(?,?,?,?,?,?)",
            (rel_id, project_id, ordn, "BOND", atom_ids[bond[0]], atom_ids[bond[1]]),
        )
        ordn += 1
    for rel in skeleton.get("relations", []):
        rel_id = next_id(conn, "rel")
        conn.execute(
            "INSERT INTO relations(id,project_id,ord,kind,a_atom_id,b_atom_id)"
            " VALUES(?,?,?,?,?,?)",
            (rel_id, project_id, ordn, rel["kind"],
             atom_ids[rel["a"]], atom_ids[rel["b"]]),
        )
        ordn += 1
    emit(
        conn, project_id, project["head_branch_id"], "skeleton_loaded",
        {"atoms": len(skeleton["atoms"]), "relations": ordn,
         "coord_system": skeleton["coord_system"]},
    )
    conditions_doc = json.loads(
        (pack_dir / manifest["conditions"]).read_text(encoding="utf-8")
    )
    commit_conditions(
        conn, project_id,
        conditions_doc["solvent"], conditions_doc["temperature"],
        conditions_doc["reference"], conditions_doc["frequency_mhz"],
    )
    for kind, filename in manifest["sources"].items():
        raw_text = (pack_dir / filename).read_text(encoding="utf-8")
        commit_source(
            conn, project_id, kind, raw_text,
            meta={"file": filename, "casepack": manifest["id"], "immutable": True},
        )
    return manifest
