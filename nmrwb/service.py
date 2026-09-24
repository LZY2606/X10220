"""Application service: the state machine of the workbench.

All mutations go through the event log. Derived results (candidate
runs) carry algo version, params fingerprint and direct source refs;
a local input change expires only the results that depend on it, and
expired results stay readable but can never pose as current ones.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from . import engine, parse
from .store import Store

MAIN = "main"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Service:
    def __init__(self, db_path: str = ":memory:", clock: Callable[[], str] | None = None,
                 seed: int = 20260919):
        self.store = Store(db_path)
        self.clock = clock or _utcnow
        self.seed = seed  # fixed seed; engine itself is fully deterministic
        if not self.store.events():
            self.store.append("project_created",
                              {"project": "nmr-assignment-workbench", "seed": seed},
                              "system", self.clock())
            self.store.append("branch_created",
                              {"branch_id": MAIN, "parent": None, "label": "main"},
                              "system", self.clock())

    # -- helpers -------------------------------------------------------
    def _peek(self) -> int:
        row = self.store.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()
        return row[0]

    def lineage(self, branch: str) -> list[str]:
        return self._lineage_from(self.store, branch)

    def latest_docs(self, store: Store | None = None) -> dict[str, dict]:
        store = store or self.store
        latest: dict[str, dict] = {}
        for d in store.table("documents"):
            if d["doc_id"] not in latest or d["version"] > latest[d["doc_id"]]["version"]:
                latest[d["doc_id"]] = d
        return latest

    def latest_params(self, store: Store | None = None) -> dict | None:
        store = store or self.store
        rows = store.table("params")
        return max(rows, key=lambda r: r["version"]) if rows else None

    def _expire(self, cand_ids: list[str], reason: str, actor: str) -> dict | None:
        if not cand_ids:
            return None
        return self.store.append("candidates_expired",
                                 {"cand_ids": sorted(cand_ids), "reason": reason},
                                 actor, self.clock())

    # -- import layer --------------------------------------------------
    def import_document(self, doc_id: str, fmt: str, raw: str, actor: str) -> dict:
        existing = [d for d in self.store.table("documents") if d["doc_id"] == doc_id]
        version = max((d["version"] for d in existing), default=0) + 1
        parent = version - 1 if version > 1 else None
        parsed = parse.parse_document(fmt, raw)
        ev = self.store.append("document_imported", {
            "doc_id": doc_id, "version": version, "parent_version": parent,
            "format": fmt, "raw": raw, "meta": parsed["meta"],
            "diagnostics": parsed["diagnostics"]}, actor, self.clock())
        # a new version expires only results derived from this document
        stale = [c["cand_id"] for c in self.store.table("candidates")
                 if c["status"] in ("current", "unknown") and doc_id in c["src_docs"]]
        exp = self._expire(stale, f"source_revised:{doc_id}:v{version}", actor)
        return {"event": ev, "version": version, "diagnostics": parsed["diagnostics"],
                "expired": exp["payload"]["cand_ids"] if exp else []}

    def commit_params(self, values: dict, affects: list[str], actor: str) -> dict:
        latest = self.latest_params()
        version = (latest["version"] + 1) if latest else 1
        merged = dict(latest["values_json"]) if latest else {}
        merged.update(values)
        fp = engine.params_fingerprint(merged)
        ev = self.store.append("params_committed", {
            "version": version, "parent_version": latest["version"] if latest else None,
            "values": merged, "affects": affects, "fingerprint": fp},
            actor, self.clock())
        # solvent / temperature / reference changes expire only candidates
        # of the affected nuclei; everything else stays current
        stale = [c["cand_id"] for c in self.store.table("candidates")
                 if c["status"] in ("current", "unknown")
                 and c["nucleus"] in affects and c["params_fp"] != fp]
        exp = self._expire(stale, f"params_revised:v{version}:{'+'.join(affects)}", actor)
        return {"event": ev, "version": version, "fingerprint": fp,
                "expired": exp["payload"]["cand_ids"] if exp else []}

    # -- domain computation --------------------------------------------
    def run_assignment(self, branch: str = MAIN, actor: str = "system") -> dict:
        docs = self.latest_docs()
        params = self.latest_params()
        if params is None:
            raise ValueError("no params committed")
        skel = next((d for d in docs.values() if d["format"] == "molskel"), None)
        if skel is None:
            raise ValueError("no molskel document")
        skel_parsed = parse.parse_document("molskel", skel["raw"])
        runs = []
        for nucleus in ("1H", "13C"):
            pdoc = next((d for d in docs.values()
                         if d["format"] == "peaklist"
                         and parse.parse_document("peaklist", d["raw"])["meta"].get("nucleus") == nucleus),
                        None)
            if pdoc is None:
                continue
            pp = parse.parse_document("peaklist", pdoc["raw"])
            src_refs = {pdoc["doc_id"]: pdoc["version"], skel["doc_id"]: skel["version"],
                        "params": params["version"]}
            run_id = f"run-{self._peek():04d}-{nucleus.replace('H', 'H')}"
            result = engine.run_assignment(
                pp["peaks"], pp["edges"], pp["missing"], skel_parsed["atoms"],
                skel_parsed["bonds"], nucleus, params["values_json"],
                [pdoc["doc_id"], skel["doc_id"]], run_id, branch)
            ev = self.store.append("assignment_run", {
                "run_id": run_id, "branch": branch, "nucleus": nucleus,
                "algo": engine.ALGO_VERSION, "params_version": params["version"],
                "params_fp": params["fingerprint"], "src_refs": src_refs,
                "candidates": result["candidates"], "unmatched": result["unmatched"]},
                actor, self.clock(), branch=branch)
            runs.append({"run_id": run_id, "nucleus": nucleus, "event_seq": ev["seq"],
                         "n_candidates": len(result["candidates"]),
                         "unmatched": result["unmatched"]})
        return {"runs": runs}

    def create_branch(self, label: str, parent: str = MAIN, actor: str = "system") -> str:
        branch = f"br-{self._peek():04d}"
        self.store.append("branch_created",
                          {"branch_id": branch, "parent": parent, "label": label},
                          actor, self.clock())
        return branch

    # -- human decisions -------------------------------------------------
    def decide(self, branch: str, peak_id: str, atom_id: str, action: str,
               actor: str) -> dict:
        if action not in ("confirm", "reject"):
            raise ValueError("action must be confirm|reject")
        new_branch = f"br-{self._peek():04d}"
        self.store.append("branch_created", {
            "branch_id": new_branch, "parent": branch,
            "label": f"{actor}:{action}:{peak_id}->{atom_id}"}, actor, self.clock())
        dec_id = f"dec-{self._peek():04d}"
        dec_ev = self.store.append("decision_made", {
            "decision_id": dec_id, "branch": new_branch, "peak_id": peak_id,
            "atom_id": atom_id, "action": action, "actor": actor},
            actor, self.clock(), branch=new_branch)
        # two-editor conflict: another live decision on the same peak by a
        # different actor with a different action
        conflicts = []
        others = [d for d in self.store.table("decisions")
                  if d["peak_id"] == peak_id and d["decision_id"] != dec_id
                  and d["actor"] != actor and d["action"] != action
                  and d["undone_seq"] is None and d["superseded_seq"] is None]
        if others:
            ids = sorted([dec_id] + [d["decision_id"] for d in others])
            already = [c for c in self.store.table("conflicts")
                       if c["status"] == "open" and c["decision_ids"] == ids]
            if not already:
                cid = f"cf-{self._peek():04d}"
                self.store.append("conflict_detected", {
                    "conflict_id": cid, "branch": new_branch, "peak_id": peak_id,
                    "decision_ids": ids}, actor, self.clock(), branch=new_branch)
                conflicts.append(cid)
        impact = self._impact(branch, peak_id, atom_id, action)
        return {"branch": new_branch, "decision_id": dec_id,
                "event_seq": dec_ev["seq"], "conflicts": conflicts, "impact": impact}

    def _impact(self, branch: str, peak_id: str, atom_id: str, action: str) -> dict:
        lineage = self.lineage(branch)
        runs = {r["run_id"]: r for r in self.store.table("runs") if r["branch"] in lineage}
        cands = [c for c in self.store.table("candidates")
                 if c["run_id"] in runs and c["peak_id"] == peak_id]
        current = [c for c in cands if c["status"] == "current"]
        chosen = [c["cand_id"] for c in current if c["atom_id"] == atom_id]
        others = [c["cand_id"] for c in current if c["atom_id"] != atom_id]
        return {"peak_id": peak_id, "action": action,
                "selected": chosen, "outranked": others,
                "expired_visible": [c["cand_id"] for c in cands if c["status"] == "expired"],
                "note": "decision recorded on new branch; expired results stay readable "
                        "but cannot pose as current"}

    def resolve_conflict(self, conflict_id: str, decision_id: str, actor: str) -> dict:
        conf = next(c for c in self.store.table("conflicts")
                    if c["conflict_id"] == conflict_id)
        superseded = [d for d in conf["decision_ids"] if d != decision_id]
        ev = self.store.append("conflict_resolved", {
            "conflict_id": conflict_id, "superseded": superseded,
            "resolution": {"kept": decision_id, "by": actor}},
            actor, self.clock(), branch=conf["branch"])
        return {"event_seq": ev["seq"], "superseded": superseded}

    def undo_decision(self, decision_id: str, actor: str) -> dict:
        ev = self.store.append("decision_undone", {"decision_id": decision_id},
                               actor, self.clock())
        return {"event_seq": ev["seq"]}

    # -- read models -----------------------------------------------------
    def _state_from(self, store: Store, branch: str) -> dict:
        lineage = self.lineage(branch) if store is self.store else self._lineage_from(store, branch)
        docs = store.table("documents")
        latest: dict[str, dict] = {}
        for d in docs:
            if d["doc_id"] not in latest or d["version"] > latest[d["doc_id"]]["version"]:
                latest[d["doc_id"]] = d
        parsed_docs = {}
        for did, d in latest.items():
            p = parse.parse_document(d["format"], d["raw"])
            parsed_docs[did] = {"doc_id": did, "version": d["version"], "format": d["format"],
                                "meta": p["meta"], "peaks": p["peaks"], "edges": p["edges"],
                                "missing": p["missing"], "atoms": p["atoms"],
                                "bonds": p["bonds"], "symmetry": p["symmetry"],
                                "values": p["values"]}
        runs = [r for r in store.table("runs") if r["branch"] in lineage]
        run_ids = {r["run_id"] for r in runs}
        candidates = [c for c in store.table("candidates") if c["run_id"] in run_ids]
        decisions = [d for d in store.table("decisions") if d["branch"] in lineage]
        # visible conclusions: latest live decision per peak in this lineage
        conclusions = {}
        for d in sorted(decisions, key=lambda x: x["event_seq"]):
            if d["undone_seq"] is None and d["superseded_seq"] is None:
                conclusions[d["peak_id"]] = d
            elif d["peak_id"] in conclusions and conclusions[d["peak_id"]]["decision_id"] == d["decision_id"]:
                del conclusions[d["peak_id"]]
        return {"branch": branch, "lineage": lineage,
                "branches": store.table("branches"),
                "events": store.events(),
                "documents": docs, "parsed": parsed_docs,
                "diagnostics": store.table("diagnostics"),
                "params": store.table("params"),
                "runs": runs, "candidates": candidates,
                "decisions": decisions,
                "conflicts": store.table("conflicts"),
                "conclusions": {k: {"decision_id": v["decision_id"], "atom_id": v["atom_id"],
                                    "action": v["action"], "actor": v["actor"]}
                                for k, v in conclusions.items()}}

    def _lineage_from(self, store: Store, branch: str) -> list[str]:
        parents = {b["branch_id"]: b["parent"] for b in store.table("branches")}
        out, cur = [], branch
        while cur is not None:
            out.append(cur)
            cur = parents.get(cur)
        return out or [MAIN]

    def state(self, branch: str = MAIN, at_seq: int | None = None) -> dict:
        if at_seq is None:
            return self._state_from(self.store, branch)
        tmp = Store(":memory:")
        tmp.replay(self.store.events(upto_seq=at_seq))
        return self._state_from(tmp, branch)

    # -- export / import ---------------------------------------------------
    def export(self) -> dict:
        return {"format": "nmrwb-export/1", "seed": self.seed,
                "events": self.store.events()}

    def import_events(self, data: dict) -> dict:
        if data.get("format") != "nmrwb-export/1":
            raise ValueError("unsupported export format")
        self.store.replay(data["events"])
        return {"imported_events": len(data["events"])}
