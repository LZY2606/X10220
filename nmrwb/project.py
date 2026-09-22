"""Event-sourced project state machine.

Rules encoded here:
  * raw sources are immutable; a revision is a new version linked to its
    parent and appears in a new branch;
  * each manual decision creates a new branch node and recomputes only the
    derived layer it depends on;
  * condition changes (solvent/temperature/reference) expire only affected
    candidates; old builds remain viewable and replayable;
  * two editors accepting contradictory assignments raise a recorded
    conflict event; resolution (or retraction) is itself history;
  * checking out any old node replays the exact source+rule+decision view at
    that point instead of showing the newest calculation.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .ids import stable_id, fingerprint, digest
from .models import (finding, FINDING_CONDITION, FINDING_CONFLICT,
                     FINDING_LOCAL, FINDING_PARAM, SEV_INFO, SEV_WARN,
                     DECISION_ACCEPT, DECISION_REJECT, DECISION_PARAM,
                     DECISION_RESOLVE, DECISION_RETRACT, DECISION_CHECKOUT,
                     CAND_VIABLE, CAND_REJECTED, CAND_UNKNOWN)
from . import parser, rules, engine


ROLE_DOC_KIND = {
    "h1": ("spectrum", "1d", "1H"),
    "c13": ("spectrum", "1d", "13C"),
    "cosy": ("spectrum", "cosy", None),
    "hsqc": ("spectrum", "hsqc", None),
    "hmbc": ("spectrum", "hmbc", None),
    "structure": ("structure", "structure", None),
    "rulepack": ("rulepack", "rulepack", None),
}
SPECTRUM_ROLES = ("h1", "c13", "cosy", "hsqc", "hmbc")
CONDITION_KEYS = ("solvent", "temperature", "reference", "frequency-MHz")
PROTON_CONDITION_KEYS = ("solvent", "temperature", "reference")


class ProjectError(ValueError):
    pass


class ProjectService:
    def __init__(self, storage, clock: Callable[[], str],
                 rng_seed: int = 1191):
        self.storage = storage
        self.clock = clock
        self.rng_seed = rng_seed

    # ---------- creation / submission ----------
    def create_project(self, title: str, project_id: Optional[str] = None
                       ) -> str:
        pid = project_id or stable_id("proj", title, self.clock(), self.rng_seed)
        self.storage.create_project(pid, title, self.clock(), self.rng_seed)
        return pid

    def submit_source(self, project_id: str, role: str, filename: str,
                      raw_text: str, editor: str = "alice",
                      parent_node_id: Optional[str] = None,
                      label: Optional[str] = None
                      ) -> Dict[str, Any]:
        if role not in ROLE_DOC_KIND:
            raise ProjectError("unknown role %r" % role)
        doc = parser.parse_document(raw_text, filename)
        if doc.has_errors():
            return {"ok": False,
                    "diagnostics": [d.to_dict() for d in doc.diagnostics
                                    if d.level == "error"]}
        sources = self.storage.list_sources(project_id)
        same_role = [s for s in sources if s["role"] == role]
        version = (max((s["version"] for s in same_role), default=0) + 1
                   if same_role else 1)
        parent_source_id = same_role[-1]["source_id"] if same_role else None
        source_id = stable_id("src", project_id, role, version,
                              fingerprint(raw_text))
        if self.storage.source_exists(source_id):
            return {"ok": False, "diagnostics": [
                {"code": "E-DUP-SOURCE", "level": "error",
                 "message": "identical source version already submitted"}]}
        source = {
            "source_id": source_id, "role": role,
            "doc_type": ROLE_DOC_KIND[role][0], "filename": filename,
            "version": version, "parent_source_id": parent_source_id,
            "raw_text": raw_text, "raw_sha256": fingerprint(raw_text),
            "parsed": doc.to_dict(), "submitted_by": editor,
            "submitted_at": self.clock(),
            "ord": len(sources),
        }
        self.storage.add_source(project_id, source)
        node = self._append_event(
            project_id, parent_node_id, "source",
            label or ("submit %s v%d" % (role, version)),
            {"type": "source", "role": role, "source_id": source_id,
             "version": version, "parent_source_id": parent_source_id,
             "filename": filename, "raw_sha256": source["raw_sha256"],
             "submitted_by": editor},
            editor, project_sources=sources)
        self.storage.touch_project(project_id, self.clock())
        return {"ok": True, "source_id": source_id, "version": version,
                "node_id": node["node_id"],
                "diagnostics": [d.to_dict() for d in doc.diagnostics]}

    # ---------- event graph ----------
    def _append_event(self, project_id, parent_node_id, kind, label, event,
                      editor, project_sources=None, branch_name=None
                      ) -> Dict[str, Any]:
        nodes = self.storage.list_nodes(project_id)
        if parent_node_id is None:
            parent_node_id = self._default_parent(nodes, project_sources)
        parent = self._node(nodes, parent_node_id) if parent_node_id else None
        branch = branch_name
        if branch is None:
            if parent is None:
                branch = "main"
            elif kind == "source" and self._has_sibling_submission(
                    nodes, parent_node_id, event.get("role")):
                branch = self._new_branch_name(nodes, parent)
            elif kind in ("decision", "resolve", "retract"):
                branch = self._new_branch_name(nodes, parent)
            else:
                branch = parent["branch"]
        node_id = stable_id("node", project_id, parent_node_id or "root",
                            kind, digest(event), self.clock())
        node = {
            "node_id": node_id, "parent_node_id": parent_node_id,
            "branch": branch, "ord": len(nodes),
            "seq": (parent["seq"] + 1) if parent else 0,
            "kind": kind, "label": label, "event": event,
            "created_by": editor, "created_at": self.clock(),
        }
        self.storage.add_node(project_id, node)
        self.storage.touch_project(project_id, self.clock())
        return node

    @staticmethod
    def _node(nodes, node_id):
        for n in nodes:
            if n["node_id"] == node_id:
                return n
        raise ProjectError("unknown node %s" % node_id)

    @staticmethod
    def _default_parent(nodes, sources):
        # newest node on main, else newest overall
        if not nodes:
            return None
        main = [n for n in nodes if n["branch"] == "main"]
        pool = main or nodes
        return max(pool, key=lambda n: n["ord"])["node_id"]

    @staticmethod
    def _has_sibling_submission(nodes, parent_id, role):
        children = [n for n in nodes if n["parent_node_id"] == parent_id]
        return any(n["event"].get("type") == "source"
                   and n["event"].get("role") == role for n in children)

    @staticmethod
    def _new_branch_name(nodes, parent):
        base = parent["branch"]
        taken = {n["branch"] for n in nodes}
        i = 1
        while True:
            name = "%s.%d" % (base, i)
            if name not in taken:
                return name
            i += 1

    # ---------- decisions ----------
    def decide(self, project_id, nucleus_id, candidate_id, verdict, editor,
               parent_node_id=None, reason="", force=False) -> Dict[str, Any]:
        if verdict not in (DECISION_ACCEPT, DECISION_REJECT):
            raise ProjectError("verdict must be accept/reject")
        ctx = self.view_at(project_id, parent_node_id)
        cand = next((c for c in ctx["build"]["candidates"]
                     if c["id"] == candidate_id and
                     c["nucleus_id"] == nucleus_id), None)
        if cand is None:
            raise ProjectError("candidate %s not found at this node"
                               % candidate_id)
        if cand["status"] == CAND_REJECTED and verdict == DECISION_ACCEPT \
                and not force:
            return {"ok": False, "conflict": "rejected-candidate",
                    "message": "candidate is rejected by scoring; "
                               "override requires force=true"}
        event = {"type": "decision", "verdict": verdict,
                 "nucleus_id": nucleus_id, "candidate_id": candidate_id,
                 "peak_id": cand["peak_id"], "editor": editor,
                 "reason": reason, "forced": bool(force and
                 cand["status"] == CAND_REJECTED),
                 "candidate_score": cand["score"],
                 "rulepack_fingerprint": cand["rulepack_fingerprint"],
                 "engine_version": cand["engine_version"]}
        node = self._append_event(project_id, parent_node_id, "decision",
                                  "%s %s -> %s" % (verdict, nucleus_id,
                                                   cand["peak_id"] or "?"),
                                  event, editor)
        conflict = self._record_editor_conflict(project_id, node)
        propagated = self._propagation_for_decision(ctx, nucleus_id,
                                                    cand["peak_id"])
        node_view = self.view_at(project_id, node["node_id"])
        return {"ok": True, "node_id": node["node_id"],
                "branch": node["branch"], "conflict": conflict,
                "propagation": propagated, "view": node_view}

    def _record_editor_conflict(self, project_id, node) -> Optional[Dict[str, Any]]:
        """Two editors accepting different peaks for the same nucleus, or the
        same peak for different nuclei, on sibling branches is a conflict."""
        nodes = self.storage.list_nodes(project_id)
        ev = node["event"]
        if ev["verdict"] != DECISION_ACCEPT:
            return None
        conflicting = None
        for other in nodes:
            if other["node_id"] == node["node_id"]:
                continue
            oev = other["event"]
            if oev.get("type") != "decision" or oev.get("verdict") != "accept":
                continue
            if oev.get("superseded"):
                continue
            if other["created_by"] == node["created_by"]:
                continue
            same_nucleus = (oev["nucleus_id"] == ev["nucleus_id"]
                            and oev["peak_id"] != ev["peak_id"])
            same_peak = (oev.get("peak_id") == ev["peak_id"]
                         and oev["nucleus_id"] != ev["nucleus_id"]
                         and ev["peak_id"] is not None)
            if same_nucleus or same_peak:
                conflicting = other
                break
        if not conflicting:
            return None
        payload = finding(
            FINDING_CONFLICT, SEV_WARN,
            "editors %s and %s recorded incompatible accepts "
            "(%s/%s vs %s/%s); no automatic winner is chosen"
            % (conflicting["created_by"], node["created_by"],
               oev_get(conflicting, "nucleus_id"),
               oev_get(conflicting, "peak_id"),
               ev["nucleus_id"], ev["peak_id"]),
            affected=[ev["nucleus_id"], oev_get(conflicting, "nucleus_id")],
            decision_nodes=[conflicting["node_id"], node["node_id"]],
            editors=[conflicting["created_by"], node["created_by"]],
            status="open")
        cnode = self._append_event(
            project_id, node["node_id"], "conflict",
            "conflict: %s / %s" % (conflicting["created_by"],
                                   node["created_by"]),
            {"type": "conflict", "finding": payload,
             "decision_nodes": payload["decision_nodes"], "status": "open"},
            "system")
        return {"finding": payload, "node_id": cnode["node_id"]}

    def resolve_conflict(self, project_id, conflict_node_id, chosen_node_id,
                         editor, parent_node_id=None, reason=""):
        conflict_node = self.storage.list_nodes(project_id)
        cnode = next((n for n in conflict_node
                      if n["node_id"] == conflict_node_id), None)
        if cnode is None or cnode["event"].get("type") != "conflict":
            raise ProjectError("not a conflict node")
        event = {"type": "resolve", "conflict_node_id": conflict_node_id,
                 "chosen_node_id": chosen_node_id, "editor": editor,
                 "reason": reason,
                 "superseded_node_ids": [n for n in
                                         cnode["event"]["decision_nodes"]
                                         if n != chosen_node_id]}
        node = self._append_event(
            project_id, parent_node_id or conflict_node_id, "resolve",
            "resolve conflict -> %s" % chosen_node_id[:12], event, editor)
        return {"ok": True, "node_id": node["node_id"],
                "branch": node["branch"]}

    def retract_decision(self, project_id, decision_node_id, editor,
                         reason=""):
        nodes = self.storage.list_nodes(project_id)
        dnode = next((n for n in nodes
                      if n["node_id"] == decision_node_id), None)
        if dnode is None or dnode["event"].get("type") != "decision":
            raise ProjectError("not a decision node")
        event = {"type": "retract", "decision_node_id": decision_node_id,
                 "editor": editor, "reason": reason}
        node = self._append_event(project_id, decision_node_id, "retract",
                                  "retract %s" % dnode["label"], event, editor)
        return {"ok": True, "node_id": node["node_id"],
                "branch": node["branch"]}

    def checkout(self, project_id, node_id, editor="reviewer"):
        """Review at an old event: produces a review branch whose view replays
        exactly that historical state."""
        nodes = self.storage.list_nodes(project_id)
        self._node(nodes, node_id)
        event = {"type": "checkout", "target_node_id": node_id,
                 "editor": editor}
        node = self._append_event(project_id, node_id, "checkout",
                                  "replay %s" % node_id[:12], event, editor)
        view = self.view_at(project_id, node["node_id"])
        return {"ok": True, "node_id": node["node_id"],
                "branch": node["branch"], "view": view}

    @staticmethod
    def _propagation_for_decision(view, nucleus_id, peak_id):
        """Compute the candidates touched by an accept/reject."""
        affected = {nucleus_id}
        for cand in view["build"]["candidates"]:
            if peak_id is not None and cand["peak_id"] == peak_id:
                affected.add(cand["nucleus_id"])
        return {"affected_nuclei": sorted(affected),
                "candidate_ids": [c["id"] for c in view["build"]["candidates"]
                                  if c["nucleus_id"] in affected]}

    # ---------- replay / views ----------
    def view_at(self, project_id, node_id=None):
        nodes = self.storage.list_nodes(project_id)
        sources = self.storage.list_sources(project_id)
        source_by_id = {s["source_id"]: s for s in sources}
        if node_id is None:
            if not nodes:
                node_id = None
            else:
                node_id = self._default_parent(nodes, sources)
        state = self._replay(nodes, source_by_id, node_id)
        node = self._node(nodes, node_id) if node_id else None
        build_rec = None
        stale = False
        if state["has_structure"] and state["rulepack"] and \
                state["has_h1"]:
            build_rec, stale = self._build(project_id, node_id, state,
                                           source_by_id)
        decisions = self._decisions_view(state["decisions"])
        branches = self._branches(nodes)
        return {
            "project_id": project_id,
            "node_id": node_id,
            "node_label": node["label"] if node else None,
            "node": node,
            "active_sources": state["active_sources"],
            "conditions": state["conditions"],
            "decisions": decisions,
            "branches": branches,
            "build": (build_rec["build"] if build_rec else None),
            "stale": stale,
            "expired_candidates": state["expired_candidates"],
            "input_fingerprint": state["input_fingerprint"],
            "findings": (build_rec["build"]["findings"]
                         if build_rec else []) + state["state_findings"],
        }

    def _replay(self, nodes, source_by_id, node_id):
        by_id = {n["node_id"]: n for n in nodes}
        chain = []
        cur = node_id
        seen = set()
        while cur and cur in by_id and cur not in seen:
            seen.add(cur)
            n = by_id[cur]
            chain.append(n)
            if n["event"].get("type") == "checkout":
                cur = n["event"]["target_node_id"]
            else:
                cur = n.get("parent_node_id")
        chain.reverse()
        active: Dict[str, str] = {}
        decisions: List[Dict[str, Any]] = []
        condition_history: Dict[str, List[Dict[str, Any]]] = {}
        state_findings: List[Dict[str, Any]] = []
        prev_conditions: Dict[str, Dict[str, str]] = {"1H": {}, "13C": {}}
        for n in chain:
            ev = n["event"]
            etype = ev.get("type")
            if etype == "source":
                role = ev["role"]
                src = source_by_id[ev["source_id"]]
                before = self._conditions_for(active, source_by_id, role)
                active[role] = ev["source_id"]
                after = self._conditions_for(active, source_by_id, role)
                if role == "rulepack":
                    state_findings.append(finding(
                        FINDING_PARAM, SEV_INFO,
                        "rule pack revised to %s (%s); old decisions keep "
                        "their parameter fingerprint and remain replayable"
                        % (src["source_id"][:12], ev.get("version")),
                        affected=[], source_id=ev["source_id"]))
                if role in SPECTRUM_ROLES:
                    affected_nucleus = {"h1": "1H", "cosy": "1H",
                                        "hsqc": "1H", "hmbc": "1H",
                                        "c13": "13C"}[role]
                    changed, old_vals, new_vals = _condition_diff(
                        before, after)
                    if changed:
                        state_findings.append(finding(
                            FINDING_CONDITION, SEV_WARN,
                            "condition change on %s: %s; only %s candidates "
                            "expire, old decisions remain replayable"
                            % (role, "; ".join(
                                "%s %r->%r" % (k, old_vals.get(k),
                                               new_vals.get(k))
                                for k in changed), affected_nucleus),
                            affected=[], nucleus=affected_nucleus,
                            changed=changed))
                    elif any(active.get(r) for r in
                             ("h1", "c13", "cosy", "hsqc", "hmbc")):
                        local_affected = self._local_propagation(
                            active, source_by_id, src)
                        state_findings.append(finding(
                            FINDING_LOCAL, SEV_INFO,
                            "local source revision %s v%s expires only "
                            "dependent %s candidates"
                            % (role, ev.get("version"),
                               ", ".join(local_affected) or "nuclei"),
                            affected=local_affected, source_id=ev["source_id"],
                            role=role))
            elif etype == "decision":
                decisions.append({"node_id": n["node_id"], **ev,
                                  "branch": n["branch"],
                                  "created_at": n["created_at"],
                                  "active": True})
            elif etype == "retract":
                target = ev["decision_node_id"]
                for d in decisions:
                    if d["node_id"] == target:
                        d["active"] = False
                        d["retracted_by"] = n["node_id"]
                state_findings.append(finding(
                    "F-DECISION-RETRACTED", SEV_INFO,
                    "decision %s retracted; history retained" % target[:12],
                    affected=[by_id[target]["event"]["nucleus_id"]]
                    if target in by_id else [],
                    node_id=n["node_id"]))
            elif etype == "resolve":
                for d in decisions:
                    if d["node_id"] in ev.get("superseded_node_ids", []):
                        d["active"] = False
                        d["superseded_by"] = n["node_id"]
                state_findings.append(finding(
                    "F-CONFLICT-RESOLVED", SEV_INFO,
                    "conflict resolved in favour of %s"
                    % ev["chosen_node_id"][:12],
                    affected=[], node_id=n["node_id"]))
        expired: Dict[str, Dict[str, Any]] = {}
        for f in state_findings:
            if f["code"] == FINDING_CONDITION:
                expired[f.get("nucleus", "1H")] = {
                    "reason": "condition", "finding": f}
        structure_id = active.get("structure")
        rule_id = active.get("rulepack")
        fp_parts = {role: sid for role, sid in sorted(active.items())}
        input_fp = "input:" + digest(fp_parts)
        conditions = self._all_conditions(active, source_by_id)
        return {
            "active_sources": active,
            "decisions": decisions,
            "state_findings": state_findings,
            "expired_candidates": expired,
            "input_fingerprint": input_fp,
            "has_structure": bool(structure_id),
            "rulepack": rule_id,
            "has_h1": bool(active.get("h1")),
            "conditions": conditions,
        }

    @staticmethod
    def _conditions_for(active, source_by_id, changed_role):
        vals = {}
        for role in SPECTRUM_ROLES:
            sid = active.get(role)
            if not sid:
                continue
            parsed = _parsed(source_by_id[sid])
            for key in CONDITION_KEYS:
                if key in parsed["header"]:
                    vals["%s:%s" % (role, key)] = parsed["header"][key]
        return vals

    @staticmethod
    def _all_conditions(active, source_by_id):
        cond = {"1H": {}, "13C": {}}
        if active.get("h1"):
            header = _parsed(source_by_id[active["h1"]])["header"]
            for key in PROTON_CONDITION_KEYS:
                if key in header:
                    cond["1H"][key] = header[key]
        if active.get("c13"):
            header = _parsed(source_by_id[active["c13"]])["header"]
            for key in CONDITION_KEYS:
                if key in header:
                    cond["13C"][key] = header[key]
        return cond

    @staticmethod
    def _local_propagation(active, source_by_id, src):
        """A revised spectrum only touches candidates that score against its
        peaks/edges.  We return nuclei ids that reference those peaks."""
        parsed = _parsed(src)
        ids = {p["id"] for p in parsed.get("peaks", [])}
        ids |= {e["id"] for e in parsed.get("edges", [])}
        # approximate affected atoms: structure-independent list returned to
        # the finding; the build's candidate ids are precise lineage.
        return sorted(ids)

    def _decisions_view(self, decisions):
        return decisions

    @staticmethod
    def _branches(nodes):
        out = {}
        for n in nodes:
            out.setdefault(n["branch"], []).append(
                {"node_id": n["node_id"], "label": n["label"],
                 "kind": n["kind"], "ord": n["ord"],
                 "parent_node_id": n.get("parent_node_id")})
        return [{"branch": b, "nodes": sorted(ns, key=lambda x: x["ord"])}
                for b, ns in sorted(out.items())]

    # ---------- derived layer ----------
    def _build(self, project_id, node_id, state, source_by_id):
        active = state["active_sources"]
        fp = state["input_fingerprint"]
        cached = self.storage.get_build(project_id, node_id or "none")
        if cached and cached["input_fingerprint"] == fp:
            stale = self._annotate(cached["build"], state)
            cached["stale"] = stale
            return cached, stale
        structure_doc = _restored(source_by_id[active["structure"]])
        rule_doc = _restored(source_by_id[active["rulepack"]])
        cfg = rules.build_config(rule_doc)
        spectra = {}
        refs = {}
        for role in SPECTRUM_ROLES:
            sid = active.get(role)
            if sid:
                spectra[role] = _restored(source_by_id[sid])
                refs[role] = sid
        conditions = state["conditions"]
        build = engine.run_build(structure_doc, cfg, spectra, conditions, refs)
        self._annotate(build, state)
        self.storage.put_build(project_id, node_id, build, fp)
        return self.storage.get_build(project_id, node_id), False

    @staticmethod
    def _annotate(build, state):
        """Apply decisions and expiry flags without mutating provenance."""
        stale = False
        by_cand = {c["id"]: c for c in build["candidates"]}
        for d in state["decisions"]:
            cand = by_cand.get(d["candidate_id"])
            if cand is None:
                continue
            if not d.get("active", True):
                continue
            cand["decision"] = {
                "verdict": d["verdict"], "editor": d["editor"],
                "node_id": d["node_id"], "reason": d.get("reason", ""),
                "forced": d.get("forced", False),
                "engine_version": d.get("engine_version"),
                "rulepack_fingerprint": d.get("rulepack_fingerprint")}
        expired = state.get("expired_candidates", {})
        for cand in build["candidates"]:
            if cand["nucleus"] in expired:
                cand["expired"] = True
                cand["expired_reason"] = expired[cand["nucleus"]]["reason"]
                stale = True
        return stale


def _restored(source_row):
    data = _parsed(source_row)
    from .models import Diagnostic
    data = dict(data)
    data["diagnostics"] = [Diagnostic(**d) for d in data.get("diagnostics", [])]
    return parser.ParsedDocument(**data)


def _parsed(source_row):
    if "parsed_json" in source_row:
        import json as _json
        return _json.loads(source_row["parsed_json"])
    return source_row["parsed"]


def _condition_diff(before, after):
    keys = set(before) | set(after)
    changed = [k for k in sorted(keys) if before.get(k) != after.get(k)]
    short_before = {k.split(":", 1)[1]: before[k] for k in changed if k in before}
    short_after = {k.split(":", 1)[1]: after[k] for k in changed if k in after}
    changed_short = sorted({k.split(":", 1)[1] for k in changed})
    return changed_short, short_before, short_after


def oev_get(node, key):
    return node["event"].get(key)

    # ---------- bundles ----------
    def export_bundle(self, project_id):
        return self.storage.export_project(project_id)

    def import_bundle(self, bundle):
        for source in bundle.get("sources", []):
            if self.storage.source_exists(source["source_id"]):
                raise ProjectError(
                    "source id %s already exists; import into a clean "
                    "workspace" % source["source_id"])
        return self.storage.import_project(bundle)
