"""Rule pack interpretation.

Rule packs version the expected chemical shifts, tolerances and scoring
weights.  A parameter fingerprint is attached to every derived value so a
parameter revision can be replayed and diffed against old decisions.
"""
from __future__ import annotations

from typing import Any, Dict

from .ids import digest

DEFAULTS = {
    "proton-tolerance-ppm": 0.25,
    "proton-hard-tolerance-ppm": 0.80,
    "carbon-tolerance-ppm": 2.5,
    "carbon-hard-tolerance-ppm": 6.0,
    "match-tolerance-ppm": 0.05,
    "bias-threshold-ppm": 0.30,
    "bias-correction": "off",
    "weight-shift": 0.55,
    "weight-mult": 0.10,
    "weight-integral": 0.10,
    "weight-network": 0.25,
    "tie-epsilon": 0.005,
}

# Numeric parameters: values are converted in the derived layer only; the
# parser keeps the raw string.  Unconvertible values keep the default and
# emit a diagnostic instead of silently corrupting scoring.
_NUMERIC = {k: float for k in DEFAULTS if k != "bias-correction"}


def build_config(doc) -> Dict[str, Any]:
    cfg: Dict[str, Any] = dict(DEFAULTS)
    diagnostics = []
    for key, raw in doc.params.items():
        if key not in DEFAULTS:
            diagnostics.append({"code": "W-UNKNOWN-PARAM",
                                "message": "unknown parameter %r" % key,
                                "raw": raw})
            continue
        if key == "bias-correction":
            cfg[key] = raw if raw in ("on", "off") else "off"
            continue
        try:
            cfg[key] = float(raw)
        except ValueError:
            diagnostics.append({"code": "W-UNCONVERTIBLE",
                                "message": "param %s=%r kept as default"
                                           % (key, raw), "raw": raw})
    cfg["expected_shifts"] = dict(doc.expected_shifts)
    cfg["conditions"] = dict(doc.conditions)
    cfg["rulepack_id"] = doc.header.get("id", "")
    cfg["rulepack_version"] = doc.header.get("version", "v1")
    cfg["diagnostics"] = diagnostics
    cfg["fingerprint"] = "rules:" + digest({
        "v": cfg["rulepack_version"],
        "params": {k: cfg[k] for k in sorted(DEFAULTS)},
        "shifts": doc.expected_shifts,
        "conditions": doc.conditions,
    })
    return cfg
