from __future__ import annotations

import copy
import hashlib
import json

DEFAULT_PARAMS = {
    "algo_version": "assign-1.0.0",
    "base_weight": 0.65,
    "evidence_weight": 0.35,
    "evidence_saturation": 0.5,
    "overlap_ppm": {"1H": 0.03, "13C": 0.3},
    "evidence_bonus": {"COSY": 0.15, "HSQC": 0.25, "HMBC": 0.10},
    "bias_threshold_ppm": {"1H": 0.03, "13C": 0.3},
    "score_round": 6,
}


def fingerprint(params: dict) -> str:
    body = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def merge_params(base: dict, patch: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_params(result[key], value)
        else:
            result[key] = value
    return result
