import json

from .engine import generate_run
from .importer import FormatError, parse_source, recompute_record_ids
from .rules import DEFAULT_PARAMS, RULES_VERSION, load_policy_registry
from .state import build_state
from .utils import Clock, param_fingerprint, stable_id


class Service:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or Clock()
        self.registry = load_policy_registry()

    # ------------------------------------------------------------------
    def _now(self):
        return self.clock.now()

    def _seq(self, project_uid):
        return self.store.next_event_seq(project_uid)

    def _emit(self, project_uid, branch_uid, kind, payload, actor="system"):
        seq = self._seq(project_uid)
        uid = stable_id("ev", (project_uid, seq))
        created = self._now()
        self.store.insert_event(uid, project_uid, branch_uid, seq, kind, actor, created, payload)
        return {"uid": uid, "seq": seq, "kind": kind, "actor": actor, "created": created,
                "payload": payload}

    def create_project(self, name, main_branch="main"):
        created = self._now()
        uid = stable_id("proj", (name, created))
        self.store.create_project(uid, name, created)
        branch_uid = stable_id("br", (uid, main_branch))
        self.store.add_branch(branch_uid, uid, main_branch, None, None, created)
        self._emit(uid, branch_uid, "project_created",
                   {"name": name, "main_branch": main_branch}, "system")
        return uid, branch_uid

    def _latest_versions(self, project_uid, branch_uid):
        state = build_state(self.store, project_uid, branch_uid)
        out = {}
        for kind, versions in state["versions_by_kind"].items():
            out[kind] = versions[-1]
        return out, state

    def import_source(self, project_uid, branch_uid, kind, filename, text, actor="importer",
                      expect_version=None):
        parsed = parse_source(kind, filename, text)
        versions = self.store.list_project_events(project_uid)
        state = build_state(self.store, project_uid, branch_uid)
        prior = state["versions_by_kind"].get(kind, [])
        version = len(prior) + 1
        if expect_version is not None and version != expect_version:
            raise FormatError("version conflict: expected v%s but next version is v%s"
                              % (expect_version, version))
        source_uid = stable_id("src", (project_uid, kind, parsed["content_hash"], version))
        parsed = recompute_record_ids(parsed, source_uid)
        parsed["uid"] = source_uid
        parsed["project_uid"] = project_uid
        parsed["version"] = version
        if prior:
            parsed["parent_version"] = prior[-1]["version"]
            parsed["parent_uid"] = prior[-1]["uid"]
        payload = {
            "source": parsed,
            "raw_text": text,
            "raw_preserved": True,
            "policy": "POL-007",
        }
        event = self._emit(project_uid, branch_uid, "source_imported", payload, actor)
        return event

    def revise_params(self, project_uid, branch_uid, params, actor, reason=""):
        merged = dict(DEFAULT_PARAMS)
        merged.update(params)
        fingerprint = param_fingerprint(merged)
        payload = {"params": merged, "fingerprint": fingerprint,
                   "rules_version": RULES_VERSION, "reason": reason, "policy": "POL-008"}
        return self._emit(project_uid, branch_uid, "param_revision", payload, actor)

    def _sources_for_run(self, state):
        sources = {}
        for kind, versions in state["versions_by_kind"].items():
            latest_uid = versions[-1]["uid"]
            ev = next(e for e in self.store.list_project_events(state["project"]["uid"])
                      if e["payload"].get("source", {}).get("uid") == latest_uid)
            sources[kind] = ev["payload"]["source"]
        return sources

    def generate(self, project_uid, branch_uid, actor="engine"):
        state = build_state(self.store, project_uid, branch_uid)
        if "molecule" not in state["sources"]:
            raise FormatError("cannot generate candidates: molecule source missing")
        if not any(k in state["sources"] for k in ("peaks_1h", "peaks_13c")):
            raise FormatError("cannot generate candidates: at least one peak list required")
        params = dict(state["params"] or DEFAULT_PARAMS)
        fingerprint = param_fingerprint(params)
        params["__fingerprint__"] = fingerprint
        sources = self._sources_for_run(state)
        versions = {kind: {"uid": s["uid"], "version": s["version"]}
                    for kind, s in sources.items()}
        sources["__versions__"] = versions
        seq = self._seq(project_uid)
        run_uid = stable_id("run", (project_uid, branch_uid, seq))
        run = generate_run(run_uid, sources, params, RULES_VERSION)
        run["seq"] = seq
        payload = {"run": run, "policy": "POL-005", "rules_version": RULES_VERSION}
        event = self._emit_raw_seq(project_uid, branch_uid, seq, "run_generated", payload, actor)
        return event

    def _emit_raw_seq(self, project_uid, branch_uid, seq, kind, payload, actor):
        uid = stable_id("ev", (project_uid, seq))
        created = self._now()
        self.store.insert_event(uid, project_uid, branch_uid, seq, kind, actor, created, payload)
        return {"uid": uid, "seq": seq, "kind": kind, "actor": actor, "created": created,
                "payload": payload}

    # ------------------------------------------------------------------
    def _conflicts_for(self, project_uid, branch_uid, candidate_uid, verdict, actor):
        state = build_state(self.store, project_uid, branch_uid)
        conflicts = []
        target = None
        target_run = None
        for run in state["runs"]:
            for cand in run["candidates"]:
                if cand["uid"] == candidate_uid:
                    target, target_run = cand, run
        if target is None:
            return conflicts, None, None
        atom_id, peak_id = target["atom_id"], target["peak_id"]
        for dec in state["decisions"]:
            if dec["verdict"] != verdict:
                continue
            other = self._candidate_by_uid(state, dec["candidate_uid"])
            if not other:
                continue
            if verdict == "accept":
                same_resource = (other["atom_id"] == atom_id or other["peak_id"] == peak_id)
                if same_resource and other["uid"] != candidate_uid:
                    conflicts.append(self._conflict("accept_same_resource", state, dec,
                                                    target, other, actor))
            else:
                if other["uid"] == candidate_uid and dec["actor"] != actor:
                    conflicts.append(self._conflict("accept_vs_reject", state, dec,
                                                    target, other, actor))
        # direct accept vs reject on the identical candidate by a different editor
        if verdict == "accept":
            for dec in state["decisions"]:
                if dec["verdict"] == "reject" and dec["candidate_uid"] == candidate_uid \
                        and dec["actor"] != actor:
                    other = self._candidate_by_uid(state, candidate_uid)
                    conflicts.append(self._conflict("accept_vs_reject", state, dec,
                                                    target, other, actor))
        return conflicts, target, target_run

    def _candidate_by_uid(self, state, uid):
        for run in state["runs"]:
            for cand in run["candidates"]:
                if cand["uid"] == uid:
                    return cand
        return None

    def _conflict(self, ctype, state, prior_decision, target, other, actor):
        key = stable_id("cf", sorted([prior_decision["uid"],
                                      "%s:%s" % (actor, target["uid"])]))
        return {"key": key, "type": ctype, "atom_id": target["atom_id"],
                "peak_id": target["peak_id"], "new_actor": actor,
                "prior_actor": prior_decision["actor"],
                "decision_uids": [prior_decision["uid"]],
                "new_candidate_uid": target["uid"],
                "prior_candidate_uid": other["uid"],
                "policy": "POL-006"}

    def review(self, project_uid, branch_uid, candidate_uid, verdict, actor, note="",
               new_branch_name=None):
        if verdict not in ("accept", "reject"):
            raise FormatError("verdict must be accept or reject")
        state = build_state(self.store, project_uid, branch_uid)
        cand = self._candidate_by_uid(state, candidate_uid)
        if cand is None:
            raise FormatError("candidate %s not found on branch" % candidate_uid)
        run_uid = None
        for run in state["runs"]:
            if any(c["uid"] == candidate_uid for c in run["candidates"]):
                run_uid = run["run_uid"]
        conflicts, target, _ = self._conflicts_for(
            project_uid, branch_uid, candidate_uid, verdict, actor)

        target_branch = branch_uid
        if new_branch_name:
            state2 = build_state(self.store, project_uid, branch_uid)
            head_seq = state2["events"][-1]["seq"] if state2["events"] else 0
            nxt_seq = self._seq(project_uid)
            br_uid = stable_id("br", (project_uid, new_branch_name, nxt_seq))
            self.store.add_branch(br_uid, project_uid, new_branch_name, branch_uid,
                                  head_seq, self._now())
            bevent = self._emit_raw_seq(project_uid, br_uid, nxt_seq, "branch_created",
                                        {"name": new_branch_name, "parent_branch": branch_uid,
                                         "forked_from_seq": head_seq}, actor)
            target_branch = br_uid

        seq = self._seq(project_uid)
        decision_uid = stable_id("dec", (project_uid, seq))
        decision = {"uid": decision_uid, "candidate_uid": candidate_uid,
                    "run_uid": run_uid, "atom_id": target["atom_id"],
                    "peak_id": target["peak_id"], "verdict": verdict, "actor": actor,
                    "note": note, "rules_version": RULES_VERSION, "policy": "POL-009"}
        event = self._emit_raw_seq(project_uid, target_branch, seq, "review_decision",
                                   {"decision": decision}, actor)
        conflict_payload = None
        if conflicts:
            cseq = self._seq(project_uid)
            conflict_payload = self._emit_raw_seq(
                project_uid, target_branch, cseq, "conflict_detected",
                {"conflicts": conflicts, "trigger_decision_uid": decision_uid,
                 "policy": "POL-006"}, actor)
        return {"decision_event": event, "branch_uid": target_branch,
                "conflicts": conflicts, "conflict_event": conflict_payload}

    def retract(self, project_uid, branch_uid, decision_uid, actor, reason=""):
        state = build_state(self.store, project_uid, branch_uid)
        if not any(d["uid"] == decision_uid for d in state["decisions"]):
            raise FormatError("decision %s not found on branch" % decision_uid)
        return self._emit(project_uid, branch_uid, "decision_retracted",
                          {"decision_uid": decision_uid, "actor": actor, "reason": reason,
                           "policy": "POL-009"}, actor)

    def resolve_conflict(self, project_uid, branch_uid, conflict_key, resolution, actor,
                         retract_decision_uids=None):
        retract_decision_uids = retract_decision_uids or []
        return self._emit(project_uid, branch_uid, "conflict_resolved",
                          {"conflict_key": conflict_key, "resolution": resolution,
                           "retract_decision_uids": retract_decision_uids,
                           "actor": actor, "policy": "POL-006"}, actor)

    # ------------------------------------------------------------------
    def state(self, project_uid, branch_uid=None, at_seq=None):
        return build_state(self.store, project_uid, branch_uid, at_seq)

    def export_project(self, project_uid, branch_uid=None, at_seq=None):
        state = build_state(self.store, project_uid, branch_uid, at_seq)
        return {
            "format": "nmr-workbench-archive",
            "format_version": 1,
            "project": state["project"],
            "branches": state["branches"],
            "events": state["events"],
            "policy_registry": self.registry,
        }

    def import_archive(self, archive):
        if archive.get("format") != "nmr-workbench-archive":
            raise FormatError("not an nmr-workbench archive")
        project = archive["project"]
        if self.store.get_project(project["uid"]):
            raise FormatError("project already present: %s" % project["uid"])
        self.store.create_project(project["uid"], project["name"], project["created"])
        for br in archive["branches"]:
            self.store.add_branch(br["uid"], br["project_uid"], br["name"],
                                  br["parent_branch_uid"], br["forked_from_event_seq"],
                                  br["created"])
        for ev in archive["events"]:
            self.store.insert_event(ev["uid"], ev["project_uid"], ev["branch_uid"], ev["seq"],
                                    ev["kind"], ev["actor"], ev["created"], ev["payload"])
        # restore per-project seq counter
        max_seq = max((ev["seq"] for ev in archive["events"]), default=0)
        key = "event_seq:%s" % project["uid"]
        if max_seq >= (self.store.get_meta(key, 0) or 0):
            self.store.set_meta(key, max_seq)
        return project["uid"]
