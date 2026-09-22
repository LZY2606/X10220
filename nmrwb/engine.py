from __future__ import annotations

from typing import List, Optional

import numpy as np

from .core import fingerprint, stable_id
from .domain import IngestError, parse_document, params_fingerprint
from .store import State, Store

ALGO_VERSION = "cand-1.0"
EVIDENCE_BONUS = {"HSQC": 10.0, "COSY": 5.0, "HMBC": 3.0}


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------ documents
    def submit_document(self, stream: str, kind: str, name: str, fmt: str,
                        raw: str, actor: str = "system") -> dict:
        state = self.store.replay()
        prev_id = state.current.get(stream)
        prev_doc = state.docs.get(prev_id) if prev_id else None
        version = len(state.doc_versions.get(stream, [])) + 1
        doc_id = stable_id("doc", stream, version, raw)
        parsed, diags = parse_document(kind, raw, doc_id)
        payload = {
            "doc_id": doc_id, "stream": stream, "kind": kind, "name": name,
            "format": fmt, "version": version, "parent": prev_id,
            "raw": raw, "parsed": parsed, "diagnostics": diags,
        }
        ev = self.store.append("document_submitted", payload, actor=actor)

        invalidation = self._apply_invalidation(state, stream, prev_doc, payload)
        run = None
        new_state = self.store.replay()
        if self._computable(new_state):
            run = self._compute(new_state, cause=ev["seq"])
        return {"event": ev, "doc_id": doc_id, "version": version,
                "diagnostics": diags, "invalidation": invalidation, "run": run}

    def _computable(self, state: State) -> bool:
        kinds = {state.docs[d]["kind"] for d in state.current.values()}
        return {"molecule", "params"} <= kinds and any(
            state.docs[d]["kind"] == "peaks" for d in state.current.values())

    def _changed_condition_scope(self, old: dict, new: dict) -> List[str]:
        scope = set()
        keys = set(old) | set(new)
        for k in keys:
            o, n = old.get(k), new.get(k)
            ov = o["value_raw"] if o else None
            nv = n["value_raw"] if n else None
            if ov != nv:
                entry = n or o
                scope.update(entry.get("affects", ["1H", "13C"]))
        return sorted(scope)

    def _apply_invalidation(self, state: State, stream: str,
                            prev_doc: Optional[dict], new_doc: dict) -> Optional[dict]:
        if prev_doc is None:
            return None
        affected, reason = [], ""
        if new_doc["kind"] == "conditions":
            scope = self._changed_condition_scope(
                prev_doc["parsed"]["conditions"], new_doc["parsed"]["conditions"])
            if not scope:
                return None
            for c in state.candidates.values():
                if c["status"] == "current" and prev_doc["doc_id"] in c["depends"] \
                        and c["nucleus"] in scope:
                    affected.append(c["cand_id"])
            reason = "conditions changed for nuclei %s; only dependent candidates expired" % scope
        else:
            for c in state.candidates.values():
                if c["status"] == "current" and prev_doc["doc_id"] in c["depends"]:
                    affected.append(c["cand_id"])
            reason = "source %s superseded by v%d" % (stream, new_doc["version"])
        if not affected:
            return None
        payload = {"affected": sorted(affected), "reason": reason,
                   "stream": stream, "superseded_doc": prev_doc["doc_id"],
                   "new_doc": new_doc["doc_id"]}
        ev = self.store.append("invalidation_applied", payload)
        return {"event_id": ev["id"], **payload}

    # ------------------------------------------------------------ candidates
    def _compute(self, state: State, cause: int) -> dict:
        docs = {state.docs[d]["kind"]: state.docs[d] for d in state.current.values()}
        molecule = docs["molecule"]["parsed"]
        params = docs["params"]["parsed"]
        conditions_doc = docs.get("conditions")
        fp = params_fingerprint(params, ALGO_VERSION)
        tol = params["tolerance_ppm"]

        peaks, peak_doc = [], {}
        for d in state.current.values():
            doc = state.docs[d]
            if doc["kind"] == "peaks":
                for pk in doc["parsed"]["peaks"]:
                    peaks.append(dict(pk, doc_id=doc["doc_id"]))
                    peak_doc[pk["peak_id"]] = doc["doc_id"]
        edges = []
        for d in state.current.values():
            doc = state.docs[d]
            if doc["kind"] == "edges":
                for e in doc["parsed"]["edges"]:
                    edges.append(dict(e, doc_id=doc["doc_id"]))

        sites = molecule["sites"]
        bonds = molecule["bonds"]
        adj = {s["site"]: set() for s in sites}
        for a, b in bonds:
            adj[a].add(b)
            adj[b].add(a)

        def path_len(a: str, b: str, limit: int = 3) -> Optional[int]:
            if a == b:
                return 0
            frontier, seen, dist = {a}, {a}, 0
            while frontier and dist < limit:
                dist += 1
                nxt = set()
                for n in frontier:
                    nxt |= adj[n]
                nxt -= seen
                if b in nxt:
                    return dist
                seen |= nxt
                frontier = nxt
            return None

        # shift-difference matrix via NumPy (deterministic, no randomness)
        usable = [pk for pk in peaks if pk["shift_ppm"] is not None]
        pred = np.array([s["pred_ppm"] for s in sites], dtype=float)
        cands = []
        by_nucleus = {}
        for pk in usable:
            by_nucleus.setdefault(pk["nucleus"], []).append(pk)
        for nuc, pk_list in by_nucleus.items():
            idx = [i for i, s in enumerate(sites) if s["nucleus"] == nuc]
            if not idx:
                continue
            shifts = np.array([p["shift_ppm"] for p in pk_list], dtype=float)
            errs = np.abs(shifts[:, None] - pred[idx][None, :])
            for pi, pk in enumerate(pk_list):
                for sj, site_i in enumerate(idx):
                    err = float(errs[pi, sj])
                    if err <= tol + 1e-9:
                        site = sites[site_i]
                        depends = [pk["doc_id"], docs["molecule"]["doc_id"],
                                   docs["params"]["doc_id"]]
                        if conditions_doc:
                            depends.append(conditions_doc["doc_id"])
                        cands.append({
                            "site": site["site"], "nucleus": nuc,
                            "peak": pk["peak_id"], "err_ppm": round(err, 6),
                            "peak_shift_ppm": pk["shift_ppm"],
                            "overlap": bool(pk["overlap_group"]),
                            "depends": depends, "evidence": [], "bonus": 0.0,
                        })

        # coupling network as evidence only — never eliminates a candidate
        by_peak = {}
        for c in cands:
            by_peak.setdefault(c["peak"], []).append(c)
        for e in edges:
            for ca in by_peak.get(e["peak_a"], []):
                for cb in by_peak.get(e["peak_b"], []):
                    if ca["nucleus"] == cb["nucleus"] and ca["site"] == cb["site"]:
                        continue
                    d = path_len(ca["site"], cb["site"])
                    if d is None:
                        continue
                    ok = (e["kind"] == "HSQC" and d == 1) or \
                         (e["kind"] == "COSY" and d == 1) or \
                         (e["kind"] == "HMBC" and 2 <= d <= 3)
                    if not ok:
                        continue
                    bonus = EVIDENCE_BONUS[e["kind"]]
                    note = "%s %s↔%s supports %s↔%s (bonds=%d)" % (
                        e["kind"], e["peak_a"], e["peak_b"], ca["site"], cb["site"], d)
                    for c in (ca, cb):
                        c["bonus"] += bonus
                        c["evidence"].append(note)
                        if e["doc_id"] not in c["depends"]:
                            c["depends"].append(e["doc_id"])

        site_sym = {s["site"]: s["symmetry_group"] for s in sites}
        sym_groups = {}
        for s in sites:
            if s["symmetry_group"]:
                sym_groups.setdefault(s["symmetry_group"], []).append(s["site"])

        out = []
        for c in cands:
            score = round(100.0 - 40.0 * min(c["err_ppm"] / tol, 1.0) + c["bonus"], 6)
            flags = []
            if c["overlap"]:
                flags.append("overlap")
            grp = site_sym[c["site"]]
            if grp and len(sym_groups.get(grp, [])) > 1:
                flags.append("symmetric")
            cid = stable_id("cand", c["site"], c["peak"], c["peak_shift_ppm"],
                            ALGO_VERSION, fp)
            out.append({
                "cand_id": cid, "site": c["site"], "nucleus": c["nucleus"],
                "peak": c["peak"], "peak_shift_ppm": c["peak_shift_ppm"],
                "err_ppm": c["err_ppm"], "score": score,
                "evidence": sorted(set(c["evidence"])), "flags": flags,
                "depends": sorted(c["depends"]),
                "algo_version": ALGO_VERSION, "fingerprint": fp,
            })

        # tie detection per site (equal top scores stay tied; nothing auto-wins)
        per_site = {}
        for c in out:
            per_site.setdefault(c["site"], []).append(c)
        for site, lst in per_site.items():
            best = max(c["score"] for c in lst)
            tops = [c for c in lst if c["score"] == best]
            if len(tops) > 1:
                for c in tops:
                    c["flags"].append("tie")

        site_status = []
        cand_sites = {c["site"] for c in out}
        for s in sites:
            if s["site"] not in cand_sites:
                site_status.append({
                    "site": s["site"], "nucleus": s["nucleus"],
                    "state": "no-candidate",
                    "reason": "no peak within tolerance %.3f ppm; kept as unknown, not forced" % tol,
                })

        run_id = stable_id("run", ALGO_VERSION, fp,
                           sorted(state.current.values()), cause)
        payload = {"run_id": run_id, "algo_version": ALGO_VERSION,
                   "fingerprint": fp, "depends": sorted(state.current.values()),
                   "cause_seq": cause, "candidates": out, "site_status": site_status}
        self.store.append("derived_computed", payload)
        return payload

    # ------------------------------------------------------------ decisions
    def _impact(self, state: State, cand: dict, verdict: str) -> List[dict]:
        impact = []
        if verdict != "accept":
            return impact
        site_sym = {s["site"]: s["symmetry_group"]
                    for d in state.current.values()
                    if state.docs[d]["kind"] == "molecule"
                    for s in state.docs[d]["parsed"]["sites"]}
        for c in state.candidates.values():
            if c["status"] != "current" or c["cand_id"] == cand["cand_id"]:
                continue
            if c["site"] == cand["site"]:
                impact.append({"object": c["cand_id"], "effect": "alternative-retained",
                               "detail": "same site; not force-removed"})
            elif c["peak"] == cand["peak"]:
                impact.append({"object": c["cand_id"], "effect": "peak-shared",
                               "detail": "same peak used by another site"})
            elif site_sym.get(c["site"]) and site_sym.get(c["site"]) == site_sym.get(cand["site"]):
                impact.append({"object": c["cand_id"], "effect": "symmetric-related",
                               "detail": "symmetry group %s" % site_sym[c["site"]]})
        return impact

    def decide(self, candidate_id: str, verdict: str, actor: str,
               note: str = "", base_branch: Optional[str] = None,
               resolves: Optional[str] = None) -> dict:
        if verdict not in ("accept", "reject", "adjust"):
            raise IngestError(["verdict must be accept/reject/adjust"])
        state = self.store.replay()
        cand = state.candidates.get(candidate_id)
        if cand is None:
            raise IngestError(["unknown candidate %s" % candidate_id])
        branch = base_branch or state.current_branch
        if branch not in state.branches:
            raise IngestError(["unknown branch %s" % branch])
        new_branch = "b%d" % len(state.branches)
        unique = not ({"overlap", "tie", "symmetric"} & set(cand["flags"]))
        impact = self._impact(state, cand, verdict)
        decision_id = stable_id("dec", candidate_id, verdict, actor,
                                branch, self.store.peek_seq())
        payload = {"decision_id": decision_id, "candidate_id": candidate_id,
                   "site": cand["site"], "verdict": verdict, "note": note,
                   "new_branch": new_branch, "impact": impact, "unique": unique,
                   "candidate_status_at_decision": cand["status"],
                   "resolves": resolves}
        ev = self.store.append("decision_made", payload, actor=actor,
                               branch=branch, switch_branch=new_branch)

        conflict = None
        ancestry = {b for b, _ in state.ancestry(branch)}
        for d in state.decisions.values():
            if d["status"] != "active" or d["actor"] == actor:
                continue
            if d["site"] != cand["site"]:
                continue
            if d.get("new_branch") in ancestry:
                continue
            if d["candidate_id"] != candidate_id or d["verdict"] != verdict:
                conflict_id = stable_id("conf", d["decision_id"], decision_id)
                cpayload = {"conflict_id": conflict_id, "site": cand["site"],
                            "decisions": [d["decision_id"], decision_id],
                            "actors": [d["actor"], actor]}
                self.store.append("conflict_detected", cpayload)
                conflict = cpayload
                break
        return {"event": ev, "decision_id": decision_id, "branch": new_branch,
                "impact": impact, "unique": unique, "conflict": conflict}

    def revert(self, decision_id: str, actor: str) -> dict:
        state = self.store.replay()
        if decision_id not in state.decisions:
            raise IngestError(["unknown decision %s" % decision_id])
        ev = self.store.append("decision_reverted", {"target": decision_id}, actor=actor)
        return {"event": ev}

    def resolve_conflict(self, conflict_id: str, resolution: str, actor: str,
                         note: str = "") -> dict:
        state = self.store.replay()
        if conflict_id not in state.conflicts:
            raise IngestError(["unknown conflict %s" % conflict_id])
        payload = {"conflict_id": conflict_id, "resolution": resolution, "note": note}
        ev = self.store.append("conflict_resolved", payload, actor=actor)
        return {"event": ev}

    def judge(self, scenario: str, basis: str, affected: List[str],
              outcome: str, actor: str = "casepack") -> dict:
        payload = {"scenario": scenario, "basis": basis,
                   "affected": sorted(affected), "outcome": outcome}
        ev = self.store.append("case_judgment", payload, actor=actor)
        return {"event": ev}
