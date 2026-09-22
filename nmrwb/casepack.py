from __future__ import annotations

import json
import os
from typing import Dict, List

from .engine import Engine

PACK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "casepack")

BASELINE = [
    ("molecule", "molecule", "molecule.json", "json"),
    ("peaks_1h", "peaks", "peaks_1h.csv", "csv"),
    ("peaks_13c", "peaks", "peaks_13c.csv", "csv"),
    ("cosy", "edges", "cosy.csv", "csv"),
    ("hsqc", "edges", "hsqc.csv", "csv"),
    ("hmbc", "edges", "hmbc.csv", "csv"),
    ("conditions", "conditions", "conditions.json", "json"),
    ("params", "params", "params.json", "json"),
]


def _read(pack_dir: str, rel: str) -> str:
    with open(os.path.join(pack_dir, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def run_narrative(engine: Engine, pack_dir: str = PACK_DIR) -> dict:
    """Replay the full case-pack narrative as an append-only event sequence."""
    summary: Dict[str, List] = {"steps": [], "judgments": []}

    def judge_from(template: dict, affected: List[str]):
        ev = engine.judge(template["scenario"], template["basis"],
                          affected, template["outcome"])
        summary["judgments"].append({"scenario": template["scenario"],
                                     "affected": sorted(affected),
                                     "event_id": ev["event"]["id"]})

    templates = {j["scenario"]: j
                 for j in json.loads(_read(pack_dir, "expected_events.json"))["judgments"]}

    # 1. baseline import — everything preserved as submitted, version 1
    for stream, kind, name, fmt in BASELINE:
        engine.submit_document(stream, kind, name, fmt, _read(pack_dir, name),
                               actor="casepack")
    state = engine.store.replay()
    baseline_ok = all(
        state.docs[state.current[s]]["version"] == 1 and
        state.docs[state.current[s]]["raw"] == _read(pack_dir, n)
        for s, _k, n, _f in BASELINE)
    assert baseline_ok, "baseline must be preserved verbatim at version 1"
    judge_from(templates["baseline-preserved"],
               [state.current[s] for s, _k, _n, _f in BASELINE])
    summary["steps"].append("baseline-imported")

    # 2. missing correlation edges — evidence weakens, candidates survive
    r = engine.submit_document("hmbc", "edges", "mutations/missing_edges/hmbc.csv",
                               "csv", _read(pack_dir, "mutations/missing_edges/hmbc.csv"),
                               actor="casepack")
    affected = r["invalidation"]["affected"] if r["invalidation"] else []
    state = engine.store.replay()
    survivors = [c for c in state.candidates.values()
                 if c["status"] == "current" and c["nucleus"] == "1H"]
    assert survivors, "candidates must survive missing edges (evidence only)"
    judge_from(templates["missing-correlation-edges"], affected)
    summary["steps"].append("missing-edges")

    # 3. global shift bias on 13C — only 13C candidates expire
    r = engine.submit_document("peaks_13c", "peaks", "mutations/shift_bias/peaks_13c.csv",
                               "csv", _read(pack_dir, "mutations/shift_bias/peaks_13c.csv"),
                               actor="casepack")
    affected = r["invalidation"]["affected"] if r["invalidation"] else []
    state = engine.store.replay()
    assert all(state.candidates[c]["nucleus"] == "13C" for c in affected)
    assert any(c["status"] == "current" and c["nucleus"] == "1H"
               for c in state.candidates.values())
    judge_from(templates["shift-bias-local-invalidation"], affected)
    summary["steps"].append("shift-bias")

    # 4. reference-standard change scoped to 13C — old decisions stay replayable
    r = engine.submit_document("conditions", "conditions",
                               "mutations/conditions_ref/conditions.json", "json",
                               _read(pack_dir, "mutations/conditions_ref/conditions.json"),
                               actor="casepack")
    affected = r["invalidation"]["affected"] if r["invalidation"] else []
    state = engine.store.replay()
    assert all(state.candidates[c]["nucleus"] == "13C" for c in affected)
    judge_from(templates["conditions-scoped-expiry"], affected)
    summary["steps"].append("conditions-scoped")

    # 5. overlapped aromatic peak + tied methyl peaks
    r = engine.submit_document("peaks_1h", "peaks", "mutations/overlap/peaks_1h.csv",
                               "csv", _read(pack_dir, "mutations/overlap/peaks_1h.csv"),
                               actor="casepack")
    state = engine.store.replay()
    diags = [d for d in state.diagnostics if d["field"] == "integral"]
    assert diags, "unconvertible integral must stay raw with a diagnostic"
    overlap_cands = [c for c in state.candidates.values()
                     if c["status"] == "current" and "overlap" in c["flags"]]
    tie_cands = [c for c in state.candidates.values()
                 if c["status"] == "current" and "tie" in c["flags"]]
    assert overlap_cands and tie_cands
    summary["steps"].append("overlap-and-tie-imported")

    # 6. accept on an overlapped, symmetric candidate — never a unique match
    target = next(c for c in overlap_cands if c["site"] == "H2")
    before_branch = state.current_branch
    dec = engine.decide(target["cand_id"], "accept", actor="reviewer",
                        note="重叠峰归属，保留全部合法候选")
    assert dec["unique"] is False
    sym_impact = [i for i in dec["impact"] if i["effect"] == "symmetric-related"]
    assert sym_impact, "symmetric partners must appear in propagation impact"
    judge_from(templates["overlap-no-forced-unique"],
               [target["cand_id"]] + [i["object"] for i in dec["impact"]])
    judge_from(templates["symmetric-atoms"], [i["object"] for i in sym_impact])
    summary["steps"].append("overlap-accepted-nonunique")

    # 7. tied best candidates stay tied
    judge_from(templates["tie-retained"], [c["cand_id"] for c in tie_cands])
    summary["steps"].append("tie-retained")

    # 8. two editors conflict on the same site, then resolve
    tie_h7 = [c for c in tie_cands if c["site"] == "H7"]
    tie_h7.sort(key=lambda c: c["peak"])
    dec_a = engine.decide(tie_h7[0]["cand_id"], "accept", actor="editor_a",
                          note="采纳 pkH3")
    dec_b = engine.decide(tie_h7[0]["cand_id"], "reject", actor="editor_b",
                          note="并列候选不应被单独否定", base_branch=before_branch)
    assert dec_b["conflict"] is not None, "editor conflict must be detected"
    conflict_id = dec_b["conflict"]["conflict_id"]
    engine.resolve_conflict(conflict_id, "retain-both-candidates",
                            actor="editor_a",
                            note="并列最优保留两个候选，不强制唯一")
    judge_from(templates["editor-conflict"],
               [dec_a["decision_id"], dec_b["decision_id"], conflict_id])
    summary["steps"].append("editor-conflict-resolved")

    # 9. parameter revision tightens tolerance — missing peaks stay unknown
    r = engine.submit_document("params", "params",
                               "mutations/params_revision/params.json", "json",
                               _read(pack_dir, "mutations/params_revision/params.json"),
                               actor="casepack")
    state = engine.store.replay()
    unknowns = [s["site"] for s in state.runs[r["run"]["run_id"]]["site_status"]]
    assert unknowns, "tightened tolerance must leave unknown sites, not forced matches"
    judge_from(templates["missing-peak-not-forced"], unknowns)
    summary["steps"].append("params-revision-unknowns")

    summary["final_branch"] = state.current_branch
    summary["events"] = len(engine.store.events())
    return summary
