"""Event-sourced workbench state machine.

Events are the only source of truth (see :mod:`nmrworkbench.store`).  Every
visible state is a *projection* of a prefix of the event log on a branch, so
returning to an old event restores the exact input versions, derived results
and human conclusions of that moment instead of overwriting history with the
latest computation.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from . import domain, ingest
from .store import Store

GLOBAL_KINDS = {"conflict", "judgment", "resolve", "undo", "input_rejected"}


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def content_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Workbench:
    def __init__(self, db_path="workbench.db", clock=None):
        self.store = Store(db_path)
        self.clock = clock or utcnow_iso
        if self.store.get_kv("init") is None:
            self.store.add_branch("main", None, 0)
            self.store.set_kv("current_branch", "main")
            self.store.set_kv("init", "1")

    def close(self):
        self.store.close()

    def is_empty(self):
        return self.store.is_empty()

    def reset(self):
        self.store.wipe()
        self.store.add_branch("main", None, 0)
        self.store.set_kv("current_branch", "main")
        self.store.set_kv("init", "1")

    # ------------------------------------------------------------ events
    def _append(self, kind, payload, branch="main", actor="user", seq=None):
        s = self.store.next_seq() if seq is None else seq
        event_id = "evt-%04d" % s
        self.store.append_event(
            event_id, branch, kind, actor, self.clock(), payload, seq=s)
        return self.store.event(event_id)

    def current_branch(self):
        return self.store.get_kv("current_branch", "main") or "main"

    def _branch_max_seq(self, branch_name):
        seq = 0
        for e in self.store.events():
            if e["branch"] == branch_name:
                seq = max(seq, e["seq"])
        return seq

    def _branch_head_seq(self, branch_name):
        # latest seq visible on the branch (mainline events up to any fork)
        p = self.project(branch=branch_name)
        return p["head_seq"]

    # ------------------------------------------------------------ inputs
    def submit_input(self, text, doc_id=None, fmt="auto", actor="user",
                     branch=None, record_error=False):
        """Submit (never overwrite) a raw input. Returns the event.

        Raises ingest.IngestError on structural format problems.  When
        ``record_error`` is true the rejected attempt is itself appended as an
        ``input_rejected`` audit event.
        """
        try:
            parsed_doc, raw = ingest.parse_document(text, declared_doc=doc_id, fmt=fmt)
        except ingest.IngestError:
            if record_error:
                self._append("input_rejected",
                             {"declared_doc": doc_id, "fmt": fmt,
                              "content_sha": content_sha(text),
                              "diagnostics": ingest.IngestError.__dict__ and None},
                             branch="*", actor=actor)
            raise
        branch = branch or self.current_branch()
        existing = [r for r in self.store.raw_inputs() if r["doc_id"] == parsed_doc]
        if existing and existing[-1]["content"] == text:
            return self.store.event(existing[-1]["event_id"])
        before = self.project(branch=branch)
        version = (existing[-1]["version"] + 1) if existing else 1
        parent_version = existing[-1]["version"] if existing else None
        event = self._append("input_submitted", {
            "doc_id": parsed_doc, "version": version,
            "parent_version": parent_version,
            "content_sha": content_sha(text),
        }, branch=branch, actor=actor)
        self.store.put_raw(parsed_doc, version, parent_version, text,
                           "json" if isinstance(raw, dict) and raw.get("format") != "text-table"
                           else "text", event["event_id"])
        after = self.project(branch=branch)
        impact = self._input_impact(before, after, parsed_doc)
        event["payload"]["impact"] = impact
        self._snapshot_derived(after, event["event_id"])
        return event

    def _snapshot_derived(self, projection, event_id):
        for cand in projection["candidates"]:
            self.store.put_derived(
                cand["candidate_id"], "candidate", cand["algo_version"],
                cand["param_fp"], cand["sources"], cand, event_id)

    # ------------------------------------------------------------ decisions
    def decide(self, atom_id, candidate_id, action, editor="reviewer",
               base_branch=None, base_seq=None):
        if action not in ("confirm", "reject"):
            raise ValueError("action must be confirm or reject")
        base_branch = base_branch or self.current_branch()
        base_seq = base_seq if base_seq is not None else self._branch_head_seq(base_branch)
        before = self.project(branch=base_branch, upto_seq=base_seq)
        ids = {c["candidate_id"] for c in before["candidates"] if c["atom_id"] == atom_id}
        if candidate_id not in ids:
            raise ValueError("candidate %s is not a current legal candidate for %s"
                             % (candidate_id, atom_id))
        decision = {
            "atom_id": atom_id,
            "candidate_id": candidate_id,
            "action": action,
            "peak_id": self._peak_of(before, candidate_id),
            "editor": editor,
        }
        new_branch = "br-%04d" % (self.store.next_seq())
        self.store.add_branch(new_branch, base_branch, base_seq)
        event = self._append("decision", decision, branch=new_branch, actor=editor)
        conflicts = self._detect_conflicts(event)
        after = self.project(branch=new_branch)
        impact = self._decision_impact(before, after, new_branch, conflicts)
        event["payload"]["impact"] = impact
        event["payload"]["new_branch"] = new_branch
        self.store.set_kv("current_branch", new_branch)
        return event, conflicts

    def resolve(self, atom_id, chosen_event_id, editor="chair"):
        target = self.store.event(chosen_event_id)
        if target is None or target["kind"] != "decision" \
                or target["payload"]["atom_id"] != atom_id:
            raise ValueError("chosen event must be a decision for %s" % atom_id)
        event = self._append("resolve", {
            "atom_id": atom_id, "chosen_event": chosen_event_id,
        }, branch="*", actor=editor)
        return event

    def undo(self, target_event_id, actor="reviewer", reason=""):
        target = self.store.event(target_event_id)
        if target is None:
            raise ValueError("unknown event %s" % target_event_id)
        event = self._append("undo", {
            "target": target_event_id, "target_kind": target["kind"],
            "reason": reason,
        }, branch="*", actor=actor)
        return event

    def judgment(self, scenario, rule, basis, affected=None, actor="casepack",
                 branch="*"):
        return self._append("judgment", {
            "scenario": scenario, "rule": rule, "basis": basis,
            "affected": affected or [],
        }, branch=branch, actor=actor)

    @staticmethod
    def _peak_of(projection, candidate_id):
        for c in projection["candidates"]:
            if c["candidate_id"] == candidate_id:
                return c["peak_id"]
        return None

    # ----------------------------------------------------------- conflicts
    def _detect_conflicts(self, new_event):
        atom = new_event["payload"]["atom_id"]
        new_branch = self.store.branches()  # noqa: F841
        branch_rows = {b["name"]: b for b in self.store.branches()}
        new_br = branch_rows[new_event["branch"]]
        fork_key = (new_br["parent"], new_br["fork_seq"])
        siblings = [b for b in self.store.branches()
                    if (b["parent"], b["fork_seq"]) == fork_key]
        undone = {e["payload"]["target"] for e in self.store.events()
                  if e["kind"] == "undo"}
        resolved = {e["payload"]["atom_id"] for e in self.store.events()
                    if e["kind"] == "resolve" and e["event_id"] not in undone}
        divergent = set()
        for b in siblings:
            for e in self.store.events():
                if e["branch"] != b["name"] or e["kind"] != "decision":
                    continue
                if e["event_id"] in undone:
                    continue
                if e["payload"]["atom_id"] != atom:
                    continue
                if e["payload"]["candidate_id"] != new_event["payload"]["candidate_id"]:
                    divergent.add(e["event_id"])
        if not divergent or atom in resolved:
            return []
        members = sorted(divergent | {new_event["event_id"]})
        for e in self.store.events():
            if e["kind"] == "conflict" and e["payload"]["atom_id"] == atom:
                if set(e["payload"]["decisions"]) == set(members):
                    return [e]
        conflict = self._append("conflict", {
            "atom_id": atom,
            "decisions": members,
            "reason": "two editors confirmed different candidates from the same parent event",
        }, branch="*", actor="system")
        return [conflict]

    @staticmethod
    def _decision_impact(before, after, new_branch, conflicts):
        b_eff = {a: e for a, e in before["effective"].items()}
        changed = []
        for atom, e in after["effective"].items():
            old = b_eff.get(atom)
            if old is None or (old["candidate_id"], old["action"], old["via"]) != (
                    e["candidate_id"], e["action"], e["via"]):
                changed.append({
                    "atom_id": atom, "action": e["action"],
                    "candidate_id": e["candidate_id"], "via": e["via"],
                })
        propagated = [
            {"atom_id": atom, "from_atom": e.get("propagated_from"),
             "peak_id": e.get("propagate_peak_id")}
            for atom, e in after["effective"].items()
            if e.get("propagated_from") and (
                atom not in b_eff or b_eff[atom].get("propagated_from") != e.get("propagated_from"))
        ]
        stale = [{"atom_id": a, "candidate_id": e["candidate_id"]}
                 for a, e in after["effective"].items() if e.get("stale")]
        return {
            "new_branch": new_branch,
            "atoms_affected": changed,
            "symmetry_propagated": propagated,
            "stale_decisions": stale,
            "conflicts": [{"atom_id": c["payload"]["atom_id"],
                           "decisions": c["payload"]["decisions"]} for c in conflicts],
        }

    @staticmethod
    def _input_impact(before, after, doc_id):
        before_status = {d["derived_id"]: d["status"] for d in before["derived_history"]}
        expired = []
        for d in after["derived_history"]:
            if d["status"] == "expired" and before_status.get(d["derived_id"], "current") != "expired":
                expired.append({"derived_id": d["derived_id"], "kind": d["kind"],
                                "sources": d["sources"]})
        stale = [{"atom_id": a, "candidate_id": e["candidate_id"]}
                 for a, e in after["effective"].items()
                 if e.get("stale") and a not in
                 {a2 for a2, e2 in before["effective"].items() if e2.get("stale")}]
        diag = {k: len(v) for k, (_, v) in after["docs"].items() if v}
        return {
            "changed_source": doc_id,
            "expired_derived": expired,
            "stale_decisions": stale,
            "diagnostics_by_doc": diag,
            "scope_note": "only derived results listing the changed source as a "
                          "direct dependency are expired",
        }

    # ------------------------------------------------------------ projection
    def project(self, branch=None, upto_seq=None, extra_events=()):
        events = self.store.events()
        branch_rows = {b["name"]: b for b in self.store.branches()}
        branch = branch or self.current_branch()
        if branch not in branch_rows:
            branch = "main"
        max_seq = events[-1]["seq"] if events else 0
        upto_seq = max_seq if upto_seq is None else min(upto_seq, max_seq)
        head_seq = self._visible_head_seq(branch, upto_seq, branch_rows, events)

        limits = self._branch_limits(branch, upto_seq, branch_rows)
        visible = [e for e in events
                   if e["seq"] <= upto_seq and self._event_visible(e, limits)]
        visible.extend(extra_events)
        visible.sort(key=lambda e: e["seq"])

        current_versions, raw_current = {}, {}
        params = {}
        decisions, undone, resolutions = [], set(), {}
        conflicts, judgments, rejected = [], [], []
        for e in visible:
            p = e["payload"]
            if e["kind"] == "input_submitted":
                current_versions[p["doc_id"]] = p["version"]
            elif e["kind"] == "decision":
                decisions.append(e)
            elif e["kind"] == "undo":
                undone.add(p["target"])
            elif e["kind"] == "resolve" and e["event_id"] not in undone:
                resolutions[p["atom_id"]] = e
            elif e["kind"] == "conflict":
                conflicts.append(e)
            elif e["kind"] == "judgment":
                judgments.append(e)
            elif e["kind"] == "input_rejected":
                rejected.append(e)
        decisions = [d for d in decisions if d["event_id"] not in undone]

        for doc_id, version in current_versions.items():
            row = self.store.raw(doc_id, version)
            if row:
                raw_current[doc_id] = row["content"]

        docs, versions, diagnostics_all = {}, {}, {}
        cond_raw = None
        if "conditions" in current_versions:
            _, craw = ingest.parse_document(raw_current["conditions"])
            cond_raw, cdiag = ingest.normalize_document("conditions", craw, None)
            docs["conditions"] = cond_raw
            versions["conditions"] = current_versions["conditions"]
            diagnostics_all["conditions"] = cdiag
        for doc_id, content in sorted(raw_current.items()):
            if doc_id == "conditions" or doc_id not in current_versions:
                continue
            _, raw = ingest.parse_document(content)
            normalized, diag = ingest.normalize_document(doc_id, raw, cond_raw)
            docs[doc_id] = normalized
            versions[doc_id] = current_versions[doc_id]
            if diag:
                diagnostics_all[doc_id] = diag
        if "params" in docs:
            params = {k: v for k, v in docs["params"].items()
                      if k not in ("doc", "format")}

        candidates = domain.compute_candidates(docs, versions, params) \
            if "skeleton" in docs else []
        cand_by_id = {c["candidate_id"]: c for c in candidates}

        effective, conflicted = self._effective_decisions(
            decisions, resolutions, conflicts, undone, candidates, docs)
        self._overlay_candidates(candidates, effective, docs)

        current_sources = {s for c in candidates for s in c["sources"]}
        derived_history = []
        for d in self.store.derived():
            status = "current" if set(d["sources"]) <= current_sources else "expired"
            row = dict(d)
            row["status"] = status
            derived_history.append(row)
        derived_history.sort(key=lambda d: d["derived_id"])

        return {
            "branch": branch,
            "upto_seq": upto_seq,
            "head_seq": head_seq,
            "branches": self.store.branches(),
            "events": visible,
            "all_events": events,
            "docs": docs,
            "versions": versions,
            "diagnostics": diagnostics_all,
            "params": params,
            "param_fp": domain.fingerprint(params) if params else None,
            "candidates": candidates,
            "candidates_by_id": cand_by_id,
            "effective": effective,
            "conflicted": conflicted,
            "conflicts": conflicts,
            "judgments": judgments,
            "rejected_inputs": rejected,
            "derived_history": derived_history,
            "undone": sorted(undone),
        }

    @staticmethod
    def _branch_limits(branch, upto_seq, branch_rows):
        limits = {}
        cur, limit = branch, upto_seq
        while cur in branch_rows:
            limits[cur] = min(limits.get(cur, limit), limit)
            row = branch_rows[cur]
            if row["parent"] is None:
                break
            limit = min(limit, row["fork_seq"])
            cur = row["parent"]
        return limits

    def _visible_head_seq(self, branch, upto_seq, branch_rows, events):
        limits = self._branch_limits(branch, upto_seq, branch_rows)
        seqs = [e["seq"] for e in events
                if e["seq"] <= upto_seq and self._event_visible(e, limits)]
        return max(seqs) if seqs else 0

    @staticmethod
    def _event_visible(event, limits):
        if event["branch"] == "*":
            return True
        return event["branch"] in limits and event["seq"] <= limits[event["branch"]]

    def _effective_decisions(self, decisions, resolutions, conflicts, undone,
                             candidates, docs):
        by_atom = {}
        for e in decisions:
            by_atom.setdefault(e["payload"]["atom_id"], []).append(e)
        all_decisions = {e["event_id"]: e for e in self.store.events()
                         if e["kind"] == "decision"}
        cand_ids = {c["candidate_id"] for c in candidates}
        peak_by_cid = {c["candidate_id"]: c["peak_id"] for c in candidates}
        effective, conflicted = {}, {}
        for atom, decs in by_atom.items():
            if atom in resolutions:
                chosen = all_decisions.get(resolutions[atom]["payload"]["chosen_event"])
                if chosen and chosen["event_id"] not in undone:
                    p = chosen["payload"]
                    cid = p["candidate_id"]
                    effective[atom] = {
                        "event_id": chosen["event_id"],
                        "candidate_id": cid,
                        "action": p["action"],
                        "peak_id": p.get("peak_id"),
                        "editor": p.get("editor", chosen["actor"]),
                        "via": "resolution",
                        "resolution_event": resolutions[atom]["event_id"],
                        "seq": chosen["seq"],
                        "stale": cid not in cand_ids,
                    }
                    continue
            confirms = [d for d in decs if d["payload"]["action"] == "confirm"]
            distinct = {d["payload"]["candidate_id"] for d in confirms}
            if len(distinct) > 1:
                conflicted[atom] = [{
                    "event_id": d["event_id"],
                    "candidate_id": d["payload"]["candidate_id"],
                    "peak_id": d["payload"].get("peak_id"),
                    "editor": d["payload"].get("editor", d["actor"]),
                    "seq": d["seq"],
                } for d in confirms]
                continue
            winner = decs[-1]
            p = winner["payload"]
            cid = p["candidate_id"]
            effective[atom] = {
                "event_id": winner["event_id"],
                "candidate_id": cid,
                "action": p["action"],
                "peak_id": p.get("peak_id"),
                "editor": p.get("editor", winner["actor"]),
                "via": "decision",
                "seq": winner["seq"],
                "stale": cid not in cand_ids,
            }
        # symmetry propagation: a confirm for one equivalent atom applies to
        # every atom in the symmetry group
        groups = docs.get("skeleton", {}).get("symmetry_groups", [])
        for group in groups:
            owns = {a: effective[a] for a in group
                    if a in effective and not effective[a].get("propagated_from")
                    and effective[a]["action"] == "confirm"
                    and not effective[a].get("stale")}
            if not owns:
                continue
            source_atom = max(owns, key=lambda a: owns[a]["seq"])
            src = owns[source_atom]
            for atom in group:
                if atom in effective:
                    continue
                target_cid = next(
                    (cid for cid, pk in peak_by_cid.items()
                     if pk == src["peak_id"]
                     and cid.startswith("cand:%s:" % atom)), None)
                if target_cid is None:
                    continue
                effective[atom] = {
                    "event_id": src["event_id"],
                    "candidate_id": target_cid,
                    "action": "confirm",
                    "peak_id": src["peak_id"],
                    "editor": src["editor"],
                    "via": "symmetry:%s" % source_atom,
                    "propagated_from": source_atom,
                    "propagate_peak_id": src["peak_id"],
                    "seq": src["seq"],
                    "stale": False,
                }
        return effective, conflicted

    @staticmethod
    def _overlay_candidates(candidates, effective, docs):
        cands = {}
        for c in candidates:
            cands.setdefault(c["atom_id"], {})[c["candidate_id"]] = c
        for atom, e in effective.items():
            c = cands.get(atom, {}).get(e["candidate_id"])
            if c is None:
                continue
            if e["action"] == "confirm":
                c["decision"] = "confirmed"
                c["decided_by"] = e["editor"]
                c["decision_event"] = e["event_id"]
                if e.get("propagated_from"):
                    c["propagated_from"] = e["propagated_from"]
            elif e["action"] == "reject":
                c["decision"] = "rejected"
                c["decided_by"] = e["editor"]
                c["decision_event"] = e["event_id"]

    # ------------------------------------------------------------ export
    def export_bundle(self):
        return self.store.export_bundle()

    def import_bundle(self, bundle):
        self.store.import_bundle(bundle)
