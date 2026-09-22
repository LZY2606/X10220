"""Reconstruct review state by replaying the append-only event log.

The database holds immutable facts (sources, generations, candidates) plus
an event log.  Nothing ever overwrites history: checking out an old event
replays only the events visible on that branch up to that sequence and
returns the conclusions *as they were then*, using the immutable
generation rows created at that time - never a fresh recomputation.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional


MAIN_BRANCH = "br_main"


def _table_dicts(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def branch_ancestors(branches: List[Dict[str, Any]], branch_id: str) -> List[str]:
    by_id = {b["branch_id"]: b for b in branches}
    chain: List[str] = []
    cur: Optional[str] = branch_id
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = by_id[cur]["parent_branch"] if cur in by_id else None
    return chain


class Snapshot:
    """Immutable view of one branch at one sequence position."""

    def __init__(self, conn: sqlite3.Connection, branch_id: str, at_seq: Optional[int]):
        self.conn = conn
        self.branches_all = _table_dicts(conn, "SELECT * FROM branches ORDER BY rowid")
        self.branch_id = branch_id
        self.ancestors = set(branch_ancestors(self.branches_all, branch_id))
        self.at_seq = at_seq
        self._reload()

    # ------------------------------------------------------------- facts
    def _reload(self) -> None:
        conn = self.conn
        self.sources = []
        for row in conn.execute("SELECT * FROM sources ORDER BY ordinal"):
            item = dict(row)
            item["parsed"] = json.loads(item["parsed_json"])
            self.sources.append(item)
        self.peaks = _table_dicts(conn, "SELECT * FROM peaks ORDER BY ordinal")
        for peak in self.peaks:
            peak["tags"] = json.loads(peak["tags_json"])
        self.edges = _table_dicts(conn, "SELECT * FROM edges ORDER BY ordinal")
        self.sites = _table_dicts(conn, "SELECT * FROM sites ORDER BY ordinal")
        self.groups = _table_dicts(conn, "SELECT * FROM groups ORDER BY ordinal")
        self.group_members: Dict[str, List[str]] = {}
        for row in conn.execute(
            "SELECT * FROM group_members ORDER BY group_id, ordinal"
        ):
            self.group_members.setdefault(row["group_id"], []).append(row["site_id"])
        self.expected_edges = _table_dicts(
            conn, "SELECT * FROM expected_edges ORDER BY ordinal"
        )
        self.generations = _table_dicts(
            conn, "SELECT * FROM generations ORDER BY ordinal"
        )
        for gen in self.generations:
            gen["params"] = json.loads(gen["params_json"])
            gen["scope"] = json.loads(gen["scope_json"])
            gen["direct"] = json.loads(gen["direct_json"])
            gen["candidates"] = _table_dicts(
                conn,
                "SELECT * FROM candidates WHERE generation_id=? ORDER BY ordinal",
                (gen["generation_id"],),
            )
            for cand in gen["candidates"]:
                cand["evidence"] = json.loads(cand["evidence_json"])
                cand["reject"] = json.loads(cand["reject_json"])
                cand["notes"] = json.loads(cand["notes_json"])
        self._replay_events()
        self._decorate()

    # ------------------------------------------------------------ events
    def visible_events(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        out = []
        for row in rows:
            event = dict(row)
            if event["branch_id"] not in self.ancestors:
                continue
            if self.at_seq is not None and event["seq"] > self.at_seq:
                continue
            event["payload"] = json.loads(event["payload_json"])
            event["parents"] = json.loads(event["parents_json"])
            out.append(event)
        return out

    def _replay_events(self) -> None:
        self.active_params: Optional[Dict[str, Any]] = None
        self.active_params_fp: Optional[str] = None
        # decisions in branch-visible order
        self.decisions: List[Dict[str, Any]] = []
        self.branch_name_by_id = {b["branch_id"]: b["name"] for b in self.branches_all}
        self.conflicts: List[Dict[str, Any]] = []
        self.merges: List[Dict[str, Any]] = []
        for event in self.visible_events():
            self._apply(event)
        # current decision per target
        self.effective: Dict[str, Dict[str, Any]] = {}
        for decision in self.decisions:
            payload = decision["payload"]
            target = payload["target_key"]
            if payload["action"] == "retract":
                self.effective.pop(target, None)
                continue
            self.effective[target] = decision

    def _apply(self, event: Dict[str, Any]) -> None:
        kind = event["kind"]
        payload = event["payload"]
        if kind == "params_revised":
            self.active_params = payload["params"]
            self.active_params_fp = payload["fingerprint"]
        elif kind == "decision":
            self.decisions.append(event)
            if payload.get("conflict"):
                self.conflicts.append(
                    {
                        "target_key": payload["target_key"],
                        "event_id": event["event_id"],
                        "conflicts_with": payload.get("conflicts_with", []),
                        "status": "open",
                    }
                )
        elif kind == "merged":
            self.merges.append(event)
            for conflict in payload.get("conflicts", []):
                self.conflicts.append(
                    {
                        "target_key": conflict["target_key"],
                        "event_id": event["event_id"],
                        "conflicts_with": conflict.get("conflicts_with", []),
                        "status": "open",
                    }
                )
        elif kind == "conflict_resolved":
            target = payload["target_key"]
            for conflict in self.conflicts:
                if conflict["target_key"] == target and conflict["status"] == "open":
                    conflict["status"] = "resolved"
                    conflict["resolution"] = payload

    # --------------------------------------------------------- decoration
    def _decorate(self) -> None:
        sources_by_id = {s["source_id"]: s for s in self.sources}
        latest_role: Dict[str, Dict[str, Any]] = {}
        for source in self.sources:
            old = latest_role.get(source["role"])
            if old is None or source["version"] > old["version"]:
                latest_role[source["role"]] = source
        self.latest_sources = latest_role
        self.conditions = None
        cond = latest_role.get("conditions")
        if cond:
            self.conditions = cond["parsed"].get("fields")

        peaks_by_id = {p["peak_id"]: p for p in self.peaks}
        for edge in self.edges:
            edge["from_peak"] = peaks_by_id.get(edge["from_peak_id"])
            edge["to_peak"] = peaks_by_id.get(edge["to_peak_id"])

        # staleness: recompute what the direct fingerprint *would* be today
        from .engine import direct_fingerprints  # local import: no cycles

        current_direct = direct_fingerprints(self.sources)
        for gen in self.generations:
            nucleus = gen["scope"]["nucleus"]
            expected = current_direct.get(nucleus, [])
            param_stale = (
                self.active_params_fp is not None
                and gen["params_fp"] != self.active_params_fp
            )
            gen["stale"] = bool(param_stale) or sorted(gen["direct"]) != sorted(expected)
            gen["stale_reasons"] = []
            if param_stale:
                gen["stale_reasons"].append("params_revised")
            if sorted(gen["direct"]) != sorted(expected):
                gen["stale_reasons"].append("direct_sources_changed")
            gen["fresh"] = not gen["stale"]
            for cand in gen["candidates"]:
                self._decorate_candidate(cand, gen)

        self.latest_generation: Dict[str, Dict[str, Any]] = {}
        for gen in self.generations:
            nucleus = gen["scope"]["nucleus"]
            self.latest_generation[nucleus] = gen

    def _decorate_candidate(self, cand: Dict[str, Any], gen: Dict[str, Any]) -> None:
        cand["generation_stale"] = gen["stale"]
        decision = None
        for event in self.decisions:
            payload = event["payload"]
            if payload.get("candidate_id") == cand["candidate_id"]:
                decision = event
        cand["decision_event_id"] = decision["event_id"] if decision else None
        cand["decision"] = decision["payload"]["action"] if decision else None
        target_decision = self.effective.get(cand["target_key"])
        cand["effective_for_target"] = bool(
            target_decision
            and target_decision["payload"].get("candidate_id") == cand["candidate_id"]
        )
        cand["accepted"] = bool(
            target_decision
            and target_decision["payload"]["action"] == "accept"
            and target_decision["payload"].get("candidate_id") == cand["candidate_id"]
        )
        cand["rejected"] = bool(decision and decision["payload"]["action"] == "reject")

    # ------------------------------------------------------------ views
    def branches_view(self) -> List[Dict[str, Any]]:
        out = []
        for branch in self.branches_all:
            out.append(
                {
                    "branch_id": branch["branch_id"],
                    "name": branch["name"],
                    "parent_branch": branch["parent_branch"],
                    "head_seq": branch["head_seq"],
                    "current": branch["branch_id"] == self.branch_id,
                    "ancestors": branch_ancestors(self.branches_all, branch["branch_id"]),
                }
            )
        return out

    def open_conflicts(self) -> List[Dict[str, Any]]:
        return [c for c in self.conflicts if c["status"] == "open"]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "branch_name": self.branch_name_by_id.get(self.branch_id),
            "at_seq": self.at_seq,
            "branches": self.branches_view(),
            "conditions": self.conditions,
            "active_params": self.active_params,
            "active_params_fp": self.active_params_fp,
            "sources": self.sources,
            "peaks": self.peaks,
            "edges": self.edges,
            "sites": self.sites,
            "groups": [
                {
                    **g,
                    "members": self.group_members.get(g["group_id"], []),
                }
                for g in self.groups
            ],
            "expected_edges": self.expected_edges,
            "generations": self.generations,
            "decisions": [
                {
                    "event_id": e["event_id"],
                    "seq": e["seq"],
                    "branch_id": e["branch_id"],
                    "kind": e["kind"],
                    "actor": e["actor"],
                    "at_iso": e["at_iso"],
                    "tick": e["tick"],
                    "payload": e["payload"],
                }
                for e in self.decisions
            ],
            "effective": {
                target: {
                    "event_id": e["event_id"],
                    "seq": e["seq"],
                    "payload": e["payload"],
                    "at_iso": e["at_iso"],
                }
                for target, e in self.effective.items()
            },
            "conflicts": self.conflicts,
            "open_conflicts": self.open_conflicts(),
            "events": [
                {
                    "event_id": e["event_id"],
                    "seq": e["seq"],
                    "branch_id": e["branch_id"],
                    "branch_name": self.branch_name_by_id.get(e["branch_id"]),
                    "kind": e["kind"],
                    "actor": e["actor"],
                    "at_iso": e["at_iso"],
                    "payload": e["payload"],
                }
                for e in self.visible_events()
            ],
        }
