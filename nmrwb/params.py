"""算法参数：带版本与指纹；参数修订产生新指纹，只使依赖结果过期。"""
from __future__ import annotations

from typing import Any, Dict

from .ident import fingerprint

PARAMS_VERSION = "params-1.0.0"

DEFAULT_PARAMS: Dict[str, Any] = {
    "tolerance_ppm": {"1H": 0.05, "13C": 0.15},
    "overlap_threshold_ppm": {"1H": 0.01, "13C": 0.08},
    "bias_correction": False,
    "min_bias_ppm": {"1H": 0.03, "13C": 0.05},
    "weights": {"multiplicity_mismatch": 0.2,
                "missing_cosy": 0.2,
                "missing_hmbc": 0.1,
                "unexpected_cosy": 0.1,
                "unexpected_hmbc": 0.05},
    "tie_rank_seed": 20260923,
}


def params_fingerprint(params: Dict[str, Any]) -> str:
    return fingerprint({"params_version": PARAMS_VERSION, "params": params})


def normalize_params(params: Dict[str, Any]) -> Dict[str, Any]:
    merged = {k: (dict(v) if isinstance(v, dict) else v)
              for k, v in DEFAULT_PARAMS.items()}
    for k, v in (params or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged
