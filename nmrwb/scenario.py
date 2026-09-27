"""Counterexample archive runner.

Each scenario is a machine-readable folder with actions and expected checks.
The checks double as acceptance assertions for the test suite.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import store
from .db import rows


def list_scenarios(pack_dir):
    pack_dir = Path(pack_dir)
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    result = []
    for scenario_id in manifest.get("scenarios", []):
        spec = json.loads(
            (pack_dir / "scenarios" / scenario_id / "scenario.json").read_text(encoding="utf-8")
        )
        result.append({
            "id": scenario_id,
            "title": spec.get("title", scenario_id),
            "summary": spec.get("summary", ""),
        })
    return result


def _current_candidate_rows(conn, project_id):
    return rows(
        conn,
        "SELECT c.*, p.label AS peak_label, a.label AS atom_label FROM candidates c "
        "JOIN peaks p ON p.id=c.peak_id JOIN atoms a ON a.id=c.atom_id "
        "WHERE c.project_id=? AND c.stale_seq IS NULL ORDER BY c.seq",
        (project_id,),
    )


def apply_scenario(conn, project_id, pack_dir, scenario_id):
    pack_dir = Path(pack_dir)
    spec_path = pack_dir / "scenarios" / scenario_id / "scenario.json"
    if not spec_path.exists():
        raise store.NotFoundError(f"scenario {scenario_id} not found")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    action_log = []
    for action in spec.get("actions", []):
        kind = action["type"]
        if kind == "commit_source":
            content = (
                pack_dir / "scenarios" / scenario_id / action["file"]
            ).read_text(encoding="utf-8")
            out = store.commit_source(conn, project_id, action["kind"], content,
                                      meta={"scenario": scenario_id, "file": action["file"]})
        elif kind == "conditions":
            out = store.commit_conditions(
                conn, project_id, action["solvent"], action["temperature"],
                action["reference"], action["frequency_mhz"]
            )
        elif kind == "compute":
            out = store.compute(conn, project_id)
        elif kind == "decision":
            find = action["find"]
            candidate = rows(
                conn,
                "SELECT c.id FROM candidates c JOIN peaks p ON p.id=c.peak_id "
                "JOIN atoms a ON a.id=c.atom_id WHERE c.project_id=? "
                "AND c.stale_seq IS NULL AND p.label=? AND a.label=? "
                "ORDER BY c.seq DESC LIMIT 1",
                (project_id, find["peak"], find["atom"]),
            ) if find else []
            base_branch = None
            if action.get("base_branch") == "main":
                main_row = rows(
                    conn,
                    "SELECT id FROM branches WHERE project_id=? AND name='main' LIMIT 1",
                    (project_id,),
                )
                base_branch = main_row[0]["id"] if main_row else None
            out = store.decide(
                conn, project_id, action["action"], action["editor"],
                candidate_id=candidate[0]["id"] if candidate else None,
                note=action.get("note", ""),
                base_branch=base_branch,
                params_patch=action.get("params"),
            )
        elif kind == "resolve":
            keep = _decision_by_editor(conn, project_id, action["keep_editor"])
            drop = _decision_by_editor(conn, project_id, action["drop_editor"])
            out = store.resolve_conflict(conn, project_id, keep["id"], drop["id"])
        elif kind == "undo":
            decision = _decision_by_editor(conn, project_id, action["editor"])
            out = store.undo_decision(conn, project_id, decision["id"])
        else:
            raise store.FormatError(f"unknown scenario action: {kind}")
        action_log.append({"type": kind, "result": out})
    checks = [_evaluate_check(conn, project_id, check) for check in spec.get("expected", [])]
    return {
        "scenario": scenario_id,
        "title": spec.get("title", scenario_id),
        "summary": spec.get("summary", ""),
        "actions": action_log,
        "checks": checks,
        "ok": all(c["passed"] for c in checks),
    }


def _decision_by_editor(conn, project_id, editor):
    found = rows(
        conn,
        "SELECT * FROM decisions WHERE project_id=? AND editor=? AND undone_seq IS NULL "
        "ORDER BY seq DESC LIMIT 1",
        (project_id, editor),
    )
    if not found:
        raise store.NotFoundError(f"no active decision by editor {editor}")
    return found[0]


def _evaluate_check(conn, project_id, check):
    kind = check["check"]
    passed = False
    detail = ""
    events = rows(conn, "SELECT type,payload FROM events WHERE project_id=? ORDER BY seq",
                  (project_id,))
    current = _current_candidate_rows(conn, project_id)
    if kind == "event_exists":
        passed = any(e["type"] == check["type"] for e in events)
        detail = f"event {check['type']}"
    elif kind == "event_payload":
        matches = [e for e in events if e["type"] == check["type"]]
        passed = False
        for event in matches:
            payload = json.loads(event["payload"])
            value = payload
            for part in check["path"].split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if value is not None and abs(value - check["approx"]) <= check["tol"]:
                passed = True
                detail = f"{check['path']}={value}"
                break
    elif kind == "candidate_min":
        matching = [c for c in current if c["peak_label"] == check["peak_label"]]
        passed = len(matching) >= check["count"]
        detail = f"{len(matching)} current candidates for {check['peak_label']}"
    elif kind == "candidate_reason":
        reasons = []
        for candidate in current:
            if candidate["peak_label"] == check["peak_label"] and candidate["atom_label"] == check["atom_label"]:
                reasons = json.loads(candidate["reasons"])
        passed = any(check["contains"] in reason for reason in reasons)
        detail = ",".join(reasons)
    elif kind == "score_equal":
        scores = {}
        for candidate in current:
            if candidate["peak_label"] == check["peak_label"] and candidate["atom_label"] in check["atom_labels"]:
                scores[candidate["atom_label"]] = candidate["score"]
        values = list(scores.values())
        passed = len(values) == len(check["atom_labels"]) and len(set(values)) == 1
        detail = json.dumps(scores, sort_keys=True)
    elif kind == "candidate_current":
        matching = [
            c for c in current
            if c["peak_label"] == check["peak_label"] and c["atom_label"] == check["atom_label"]
        ]
        passed = bool(matching) == check.get("present", True)
        detail = f"{len(matching)} current"
    elif kind == "stale_count":
        count = rows(
            conn,
            "SELECT COUNT(*) AS n FROM candidates WHERE project_id=? AND stale_seq IS NOT NULL",
            (project_id,),
        )[0]["n"]
        passed = count >= check["min"]
        detail = f"{count} stale candidates"
    elif kind == "conflict_open":
        snapshot = store.state(conn, project_id)
        passed = len(snapshot["conflicts"]) == check["count"]
        detail = f"{len(snapshot['conflicts'])} open conflicts"
    elif kind == "effective_decision":
        snapshot = store.state(conn, project_id)
        target = None
        for candidate in snapshot["candidates"]:
            if candidate["peak_label"] == check["peak_label"] and candidate["atom_label"] == check["atom_label"]:
                target = candidate["id"]
        decision_id = snapshot["effective_decisions"].get(target) if target else None
        action = None
        if decision_id:
            decision = next(d for d in snapshot["decisions"] if d["id"] == decision_id)
            action = decision["action"]
        passed = action == check["action"]
        detail = f"effective action={action}"
    else:
        detail = f"unknown check {kind}"
    return {"check": check, "passed": passed, "detail": detail}
