"""Bundled offline domain case pack loader.

Plays a single connected review narrative (not disconnected buttons):
baseline import -> review -> parameter revision -> global-bias source revision
-> local condition-only change -> two-editor conflict on one nucleus ->
resolution. All events land on the same project so any point can be replayed.
"""
from __future__ import annotations

import json
import os
from typing import Tuple

from .store import EventStore, Clock
from .service import WorkbenchService

CASE_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "cases", "nmr_resonance_atlas")


def load_manifest(root=CASE_ROOT):
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def build_case_project(store: EventStore, project_id="case-resonance-atlas",
                       root=CASE_ROOT, clock: Clock = None):
    """Replay the fixed case narrative. Deterministic with a fixed Clock."""
    if clock is None:
        clock = Clock(start=1_700_000_000.0, step=1.0)
    store.clock = clock
    svc = WorkbenchService(store)
    manifest = load_manifest(root)
    svc.create_project(project_id, manifest["title"], now=clock.now())

    def submit(entry, author="importer", branch="main"):
        text = _read(root, entry["file"])
        ev, doc = svc.submit_source(project_id, entry["logical_id"], text,
                                    os.path.basename(entry["file"]),
                                    author=author, branch=branch)
        return ev, doc

    baseline_events = []
    for entry in manifest["baseline"]:
        ev, doc = submit(entry)
        baseline_events.append((ev, doc))

    # Editor A accepts a confident single assignment on the baseline.
    ev_a, _, _ = svc.decide(project_id, "H1", "accepted", "Hp1",
                            reason="acetyl methyl, singlet, HSQC to C1",
                            author="editor-alice")

    # Parameter revision (tighter proton tolerance), then biased re-reference.
    ev_p = svc.set_parameters(
        project_id, {"shift_tolerance": {"1H": 0.05, "13C": 0.5}},
        reason="tuned after calibration run", author="editor-alice")
    rev2 = manifest["revisions"][0]
    ev_v2, doc_v2 = submit(rev2)
    ev_p2 = svc.set_parameters(
        project_id, {"shift_tolerance": {"1H": 0.05, "13C": 0.5}},
        reason="re-assert tolerance after bias-fit", author="editor-alice")

    # Editor B disagrees with A on a methyl assignment, at the same candidate
    # network state -> conflict fork; both sides are retained.
    ev_b, new_branch, prior = svc.decide(
        project_id, "H1", "rejected", "Hp1",
        reason="second reader prefers unresolved acetyl assignment",
        author="editor-bob", allow_conflict_branch=True)
    conflict_branch = new_branch

    # Lead reviewer resolves on main, keeping editor A's decision.
    ev_res = svc.resolve_conflict(
        project_id, "H1", "accepted", "Hp1",
        resolution="kept_first_editor", winner_event=ev_a["event_id"],
        loser_event=ev_b["event_id"], author="lead-carol")

    # Condition-only change (solvent/temperature/reference): candidates expire
    # but no prior decision is overwritten; replay of earlier seq still works.
    ev_v3, doc_v3 = submit(manifest["revisions"][1])

    return {
        "project_id": project_id,
        "manifest": manifest,
        "baseline_events": [e["event_id"] for e, _ in baseline_events],
        "editor_a_event": ev_a["event_id"],
        "parameter_event": ev_p["event_id"],
        "bias_source_event": ev_v2["event_id"],
        "editor_b_event": ev_b["event_id"],
        "conflict_branch": conflict_branch,
        "resolution_event": ev_res["event_id"],
        "condition_event": ev_v3["event_id"],
        "baseline_docs": [d for _, d in baseline_events],
        "v2_doc": doc_v2,
        "v3_doc": doc_v3,
    }
