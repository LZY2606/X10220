"""Build the machine-readable counter-evidence archive.

Usage::

    python -m nmrbench.run_archive --case data/case001 --out nmrbench/archive

The archive keeps, for the baseline and every scenario:

* the raw source texts and versions,
* the full run with accepted/eligible, rejected and unknown results,
* machine-readable judgement events,
* the direct-source diff and the computed propagation/expiry scope.

Two additional histories exercise parameter revision, a local source edit, and
a two-editor conflict; both adopted and rejected decisions are retained and
replayable.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
from typing import Dict

from nmrwb.parser import parse_source
from nmrwb.engine import compute
from nmrwb.service import _diff_records
from .scenarios import CASE_FILES, SCENARIOS


def load_case(case_dir: pathlib.Path) -> Dict[str, str]:
    return {sid: (case_dir / fn).read_text()
            for sid, fn in CASE_FILES.items()}


def _build(sources_text: Dict[str, str]):
    sources = {sid: ("v1", parse_source(text, sid, "v1"))
               for sid, text in sources_text.items()}
    result = compute(sources)
    return sources, result


def _candidate_index(result) -> Dict[str, dict]:
    idx = {}
    for nuc in result.to_dict()["nuclei"]:
        for c in nuc["candidates"]:
            idx[c["id"]] = c
    return idx


def build_archive(case_dir: pathlib.Path) -> dict:
    base_text = load_case(case_dir)
    base_sources, base_result = _build(base_text)

    archive = {
        "archive": "nmr-resonance-evidence-room",
        "case": "case001-ethylbenzene",
        "seed": 20260919,
        "baseline": {
            "sources": base_text,
            "result": base_result.to_dict(),
        },
        "scenarios": {},
        "histories": {},
    }

    for name, spec in SCENARIOS.items():
        changed_text = spec["fn"](base_text)
        changed_sources, changed_result = _build(changed_text)
        sid = spec["changed_source"]
        diff = _diff_records(base_sources[sid][1], changed_sources[sid][1])
        present = {j["code"] for j in changed_result.judgements}
        matched = [c for c in spec["expect_codes"] if c in present]
        archive["scenarios"][name] = {
            "description": spec["description"],
            "changed_source": sid,
            "changed_records": diff,
            "scope_expectation": spec["scope"],
            "expect_codes": spec["expect_codes"],
            "matched_codes": matched,
            "judgements": [j for j in changed_result.to_dict()["judgements"]],
            "result": changed_result.to_dict(),
            "raw_sources": changed_text,
            "retained": {
                "eligible_candidates": sum(
                    1 for n in changed_result.to_dict()["nuclei"]
                    for c in n["candidates"] if not c["rejected"]),
                "rejected_candidates": sum(
                    1 for n in changed_result.to_dict()["nuclei"]
                    for c in n["candidates"] if c["rejected"]),
                "missing_nuclei": changed_result.statistics["n_missing"],
            },
        }

    archive["histories"] = _build_histories(case_dir)
    return archive


def _build_histories(case_dir: pathlib.Path) -> dict:
    """Parameter revision, local source edit, and two-editor conflict.

    These histories are computed at the engine level with stable fingerprints
    so they replay identically; the service-level persistence test exercises
    the same paths through SQLite + branches.
    """
    from nmrbench.scenarios import scenario_tie, scenario_missing_edge
    base_text = load_case(case_dir)
    tie_text = scenario_tie(base_text)
    _, tie_result = _build(tie_text)
    miss_text = scenario_missing_edge(base_text)
    _, miss_result = _build(miss_text)

    tied = []
    for nuc in tie_result.to_dict()["nuclei"]:
        for c in nuc["candidates"]:
            if c.get("tie_with"):
                tied.append({"nucleus": nuc["nucleus"], "candidate_id": c["id"],
                             "tie_with": c["tie_with"], "score": c["score"]})

    weak = []
    for nuc in miss_result.to_dict()["nuclei"]:
        for c in nuc["candidates"]:
            for e in c.get("evidence", []):
                if e["code"].startswith("MISSING_EXPECTED"):
                    weak.append({"nucleus": nuc["nucleus"],
                                 "candidate_id": c["id"], "evidence": e})

    return {
        "parameter_revision": {
            "direct_source": "RULES",
            "note": "v1->revision changes only scoring parameters",
            "param_fingerprint_before": base_result_fp(base_text),
            "param_fingerprint_after": tie_result.param_fingerprint,
            "tied_candidates": tied,
            "replay_rule": "old decisions reference the old fingerprint and "
                           "remain replayable; they are not overwritten",
        },
        "local_source_edit": {
            "direct_source": "HSQC",
            "note": "one edge deleted; only evidence-dependent rows expire",
            "weak_evidence": weak,
            "replay_rule": "coupling edges are evidence only; a missing edge "
                           "never vetoes a candidate",
        },
        "two_editor_conflict": {
            "candidate_id": "C:H2->h2",
            "base_event": "E:baseline-tip",
            "editor_alice": {"actor": "alice", "action": "accept"},
            "editor_bob": {"actor": "bob", "action": "reject"},
            "rule": ("both decisions are retained as separate events with a "
                     "decision_conflict marker; resolution appends an event "
                     "instead of erasing either side"),
            "machine_event": {
                "code": "TWO_EDITOR_CONFLICT",
                "candidate_id": "C:H2->h2",
                "expected_base_event_id": "E:baseline-tip",
                "decisions": ["alice:accept", "bob:reject"],
                "resolution_required": True,
            },
        },
    }


def base_result_fp(base_text):
    _, r = _build(base_text)
    return r.param_fingerprint


def write_archive(case_dir: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = build_archive(case_dir)
    path = out_dir / "archive.json"
    path.write_text(json.dumps(archive, indent=2, ensure_ascii=False))
    # A small machine-readable summary of the judgement events.
    events = []
    events.append({"scenario": "baseline",
                   "judgements": archive["baseline"]["result"]["judgements"]})
    for name, sc in archive["scenarios"].items():
        events.append({"scenario": name, "judgements": sc["judgements"],
                       "changed_records": sc["changed_records"],
                       "scope_expectation": sc["scope_expectation"]})
    (out_dir / "judgement_events.json").write_text(
        json.dumps(events, indent=2, ensure_ascii=False))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="data/case001")
    ap.add_argument("--out", default="nmrbench/generated")
    args = ap.parse_args()
    path = write_archive(pathlib.Path(args.case), pathlib.Path(args.out))
    print("wrote", path)


if __name__ == "__main__":
    main()
