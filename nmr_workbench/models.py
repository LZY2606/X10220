from __future__ import annotations

from typing import Any, Literal


SourceKind = Literal["structure", "1H", "13C", "COSY", "HSQC", "HMBC", "acquisition"]
DecisionAction = Literal["accept", "reject"]


def initial_state(project_id: str = "project", title: str = "Untitled NMR project") -> dict[str, Any]:
    return {
        "project_id": project_id,
        "title": title,
        "sources": {},
        "parameters": {
            "parameter_id": "param_default",
            "algorithm_version": "nmr-assignment-1.0.0",
            "global_shift_bias_ppm": 0.0,
            "proton_tolerance_ppm": 0.18,
            "carbon_tolerance_ppm": 0.45,
            "weight_shift": 1.0,
            "weight_multiplicity": 0.5,
            "weight_cosy": 0.2,
            "weight_hsqc": 0.7,
            "weight_hmbc": 0.4,
            "missing_edge_penalty": 0.12,
            "tie_tolerance": 1e-9,
            "random_seed": 260928,
            "fingerprint": "default:260928",
        },
        "candidates": {},
        "candidate_order": [],
        "decisions": {},
        "branch_decisions": {},
        "conflicts": {},
        "judgments": [],
        "branch": "main",
        "head": None,
        "branches": {
            "main": {
                "branch_id": "main",
                "name": "main",
                "head_event_id": None,
                "parent_branch_id": None,
                "created_by_event_id": None,
            }
        },
        "branch_seq": {"main": 0},
    }
