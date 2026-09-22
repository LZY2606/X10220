import json
import os

RULES_VERSION = "rules-2026.09.0"

DEFAULT_PARAMS = {
    "shift_tolerance_h_ppm": 0.35,
    "shift_tolerance_c_ppm": 1.8,
    "overlap_window_h_ppm": 0.04,
    "hsqc_xy_tol_ppm": [0.05, 1.2],
    "cosy_tol_ppm": 0.05,
    "hmbc_tol_ppm": [0.12, 2.5],
    "bias_threshold_ppm": 0.12,
    "missing_edge_penalty": 0.45,
    "weight_shift": 0.55,
    "weight_edge": 0.45,
}


def load_policy_registry():
    from .constants import POLICIES
    return {
        "rules_version": RULES_VERSION,
        "policies": POLICIES,
        "default_params": DEFAULT_PARAMS,
    }
