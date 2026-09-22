"""Orchestration: imports, parameter revisions, computation, decisions,
conflicts, undo, replay, export/import. Every human or machine action is an
append-only event; human confirm/reject opens a new branch and reports its
propagation impact."""
from __future__ import annotations

import json
from pathlib import Path

from .engine import (ALGO_VERSION, PARAM_DEFAULTS, PARAM_SCOPE, POLICY_GUARDS,
                     fingerprint, generate_candidates)
from .parse import parse_peaklist
from .store import Store

CASEPACK_DIR = Path(__file__).resolve().parent.parent / "casepack"


def default_skeleton():
    return json.loads((CASEPACK_DIR / "molecule.json").read_text(encoding="utf-8"))


def _changed_sections(old_parsed, new_parsed):
    changed = []
    if old_parsed["meta"] != new_parsed["meta"]:
        changed.append("meta")
    for key, section in [("protons", "1H"), ("carbons", "13C"),
                         ("hsqc", "HSQC"), ("cosy", "COSY"), ("hmbc", "HMBC")]:
        if old_parsed[key] != new_parsed[key]:
            changed.append(section)
    return changed


class Workbench:
    def __init__(self, store: Store):
        self.store = store

    # ---------------- project ----------------
    def create_project(self, name, skeleton=None):
        pid = self.store.create_project(name, skeleton or default_skeleton())
        self.store.add_event(pid, "create_project", {"name": name}, actor="system")
        return pid

    # ---------------- import ----------------
    def import_peaklist(self, project_id, raw_text, actor="system", kind="peaklist"):
        parsed = parse_peaklist(raw_text)
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError("unknown project %s" % project_id)

        state = self.store.replay(project_id)
        prev_inputs = [i for i in state["inputs"].values() if i["kind"] == kind]
        prev_input = self.store.get_input(prev_inputs[-1]["id"]) if prev_inputs else None

        ev_import = self.store.add_event(
            project_id, "import_input",
            {"kind": kind, "diagnostics": parsed["diagnostics"], "ok": parsed["ok"],
             "parse_version": parsed["parse_version"],
             "input_id": "PENDING"},
            actor=actor)
        inp = self.store.add_input(project_id, kind, raw_text, parsed, ev_import["id"])
        # patch the event payload with the real input id (append a correction
        # would also work; here we simply record it in the compute event too)
        self.store.conn.execute("UPDATE events SET payload=? WHERE id=?",
                                (json.dumps({"kind": kind,
                                             "diagnostics": parsed["diagnostics"],
                                             "ok": parsed["ok"],
                                             "parse_version": parsed["parse_version"],
                                             "input_id": inp["id"]}), ev_import["id"]))
        self.store.conn.commit()

        impact = {"invalidated": [], "guards": []}
        if prev_input is not None:
            changed = _changed_sections(prev_input["parsed"], parsed)
            impact["invalidated"] = self._invalidate_by_sections(
                project_id, changed, reason="source-change",
                detail={"input_id": inp["id"], "parent_input_id": prev_input["id"],
                        "changed_sections": changed})
        compute = self._compute(project_id, inp["id"], actor)
        impact["guards"] = compute["guards"]
        return {"input": inp, "event": ev_import, "diagnostics": parsed["diagnostics"],
                "ok": parsed["ok"], "impact": impact, "compute_event": compute["event"]}

    # ---------------- parameters ----------------
    def revise_parameters(self, project_id, changes, actor="system"):
        unknown = [k for k in changes if k not in PARAM_DEFAULTS]
        if unknown:
            raise ValueError("unknown parameters: %s" % ", ".join(unknown))
        ev = self.store.add_event(project_id, "revise_parameters",
                                  {"changes": changes}, actor=actor)
        kinds = {PARAM_SCOPE.get(k, "all") for k in changes}
        state = self.store.replay(project_id)
        affected = []
        for did, rec in state["derived"].items():
            if rec["status"] != "current":
                continue
            atom_kind = rec["payload"]["kind"]
            if "all" in kinds or atom_kind in kinds \
                    or ("proton" in kinds and atom_kind == "proton") \
                    or ("carbon" in kinds and atom_kind == "carbon"):
                affected.append(did)
        invalidated = self._invalidate(project_id, affected,
                                       reason="parameter-change",
                                       detail={"changed_params": sorted(changes)})
        # recompute on the newest input with the new parameters
        state = self.store.replay(project_id)
        latest_input = self._latest_input_id(state)
        compute = self._compute(project_id, latest_input, actor) if latest_input else None
        return {"event": ev, "invalidated": invalidated,
                "guards": compute["guards"] if compute else []}

    # ---------------- decisions ----------------
    def decide(self, project_id, atom_id, action, candidate_id=None,
               actor="anonymous", note=None):
        if action not in ("accept", "reject", "adjust"):
            raise ValueError("action must be accept|reject|adjust")
        ev = self.store.add_event(
            project_id, "decide",
            {"atom_id": atom_id, "action": action,
             "candidate_id": candidate_id, "note": note},
            actor=actor, branch_id="br-%s" % atom_id.lower())
        state = self.store.replay(project_id)
        open_conflicts = [c for c in state["conflicts"].values() if c["status"] == "open"]
        atom_conflicts = [c for c in open_conflicts if c["atom_id"] == atom_id]
        return {"event": ev, "branch_id": ev["branch_id"],
                "impact": {
                    "atom": state["decisions"].get(atom_id),
                    "conflicts": atom_conflicts,
                    "affected_derived": [d for d, r in state["derived"].items()
                                         if r["atom_id"] == atom_id],
                }}

    def resolve_conflict(self, project_id, conflict_id, adopted_event_id, actor="anonymous"):
        state = self.store.replay(project_id)
        if conflict_id not in state["conflicts"]:
            raise KeyError("unknown conflict %s" % conflict_id)
        ev = self.store.add_event(
            project_id, "resolve_conflict",
            {"conflict_id": conflict_id, "adopted_event_id": adopted_event_id},
            actor=actor)
        return {"event": ev, "conflict": self.store.replay(project_id)["conflicts"][conflict_id]}

    def undo(self, project_id, target_event_id, actor="anonymous"):
        ev = self.store.add_event(project_id, "undo",
                                  {"target_event_id": target_event_id}, actor=actor)
        return {"event": ev}

    # ---------------- state / export ----------------
    def state(self, project_id, at_event_id=None):
        return self.store.replay(project_id, at_event_id)

    def export_project(self, project_id):
        return self.store.export_project(project_id)

    def import_project(self, data):
        return self.store.import_project(data)

    # ---------------- internals ----------------
    def _latest_input_id(self, state):
        if not state["inputs"]:
            return None
        return sorted(state["inputs"].values(),
                      key=lambda i: (i["kind"], i["version"]))[-1]["id"]

    def _invalidate_by_sections(self, project_id, changed_sections, reason, detail):
        if not changed_sections:
            return []
        state = self.store.replay(project_id)
        affected = []
        for did, rec in state["derived"].items():
            if rec["status"] != "current":
                continue
            deps = set(rec["payload"]["depends_on"]["sections"])
            if "meta" in changed_sections or deps & set(changed_sections):
                affected.append(did)
        return self._invalidate(project_id, affected, reason=reason, detail=detail)

    def _invalidate(self, project_id, derived_ids, reason, detail):
        if not derived_ids:
            return []
        payload = {"affected_derived_ids": sorted(derived_ids),
                   "reason": reason, "detail": detail}
        self.store.add_event(project_id, "invalidate", payload, actor="system")
        return sorted(derived_ids)

    def _compute(self, project_id, input_id, actor):
        project = self.store.get_project(project_id)
        inp = self.store.get_input(input_id)
        state = self.store.replay(project_id)
        params = state["params"]
        fp = fingerprint(params)
        result = generate_candidates(project["skeleton"], inp["parsed"], params)
        derived = {}
        for atom_id, record in result["atoms"].items():
            did = self.store.add_derived(
                project_id, input_id, atom_id, ALGO_VERSION, fp,
                {"input_id": input_id, "algo_version": ALGO_VERSION,
                 "param_fingerprint": fp, **record},
                event_id="PENDING")
            derived[did] = {"atom_id": atom_id, "input_id": input_id,
                            "algo_version": ALGO_VERSION,
                            "param_fingerprint": fp, "payload":
                            {"input_id": input_id, "algo_version": ALGO_VERSION,
                             "param_fingerprint": fp, **record}}
        ev = self.store.add_event(
            project_id, "compute",
            {"input_id": input_id, "algo_version": ALGO_VERSION,
             "param_fingerprint": fp, "params": params,
             "derived": derived, "guards": result["guards"],
             "policy": POLICY_GUARDS},
            actor=actor)
        # point derived rows at their compute event
        for did in derived:
            self.store.conn.execute("UPDATE derived SET event_id=? WHERE id=?",
                                    (ev["id"], did))
        self.store.conn.commit()
        return {"event": ev, "guards": result["guards"], "derived_ids": sorted(derived)}

    # ---------------- offline case package ----------------
    def load_casepack(self, project_id):
        """Run the scripted 'NMR 共振经纬' scenario as real events."""
        summary = {"steps": []}

        def step(name, **info):
            summary["steps"].append(dict(step=name, **info))

        baseline = (CASEPACK_DIR / "baseline.peaklist.txt").read_text(encoding="utf-8")
        r1 = self.import_peaklist(project_id, baseline, actor="system")
        step("import-baseline-v1", input=r1["input"]["id"], guards=len(r1["impact"]["guards"]))

        r2 = self.decide(project_id, "H2", "accept", "H2~pH1", actor="alice",
                         note="ortho proton matches pH1")
        step("alice-accept-H2", branch=r2["branch_id"])

        r3 = self.revise_parameters(project_id, {"reference": "TMS-lotB"}, actor="alice")
        step("revise-reference", invalidated=len(r3["invalidated"]))

        offset = (CASEPACK_DIR / "offset.peaklist.txt").read_text(encoding="utf-8")
        r4 = self.import_peaklist(project_id, offset, actor="system")
        step("import-offset-v2", input=r4["input"]["id"],
             invalidated=len(r4["impact"]["invalidated"]),
             guards=[g["rule"] for g in r4["impact"]["guards"]])

        missing = (CASEPACK_DIR / "missing_edge.peaklist.txt").read_text(encoding="utf-8")
        r5 = self.import_peaklist(project_id, missing, actor="system")
        step("import-missing-edge-v3", input=r5["input"]["id"],
             invalidated=r5["impact"]["invalidated"])

        r6 = self.decide(project_id, "H4", "accept", "H4~pH2", actor="alice",
                         note="highest score under biased reference")
        step("alice-accept-H4", branch=r6["branch_id"])
        r7 = self.decide(project_id, "H4", "accept", "H4~pH3", actor="bob",
                         note="shift 7.25 matches para site after offset correction")
        conflicts = r7["impact"]["conflicts"]
        step("bob-conflicts-H4", conflicts=[c["id"] for c in conflicts])

        if conflicts:
            cid = conflicts[0]["id"]
            adopted = conflicts[0]["parties"][1]["event_id"]
            r8 = self.resolve_conflict(project_id, cid, adopted, actor="carol")
            step("carol-resolves", conflict=cid, adopted=adopted)
        return summary
