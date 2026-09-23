"""Assignment rule parameters with content fingerprints."""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Any, Optional

DEFAULT_TOLERANCE_PPM = {"1H": 0.18, "13C": 3.0}
DEFAULT_OVERLAP_RESOLUTION_PPM = {"1H": 0.03, "13C": 0.05}
DEFAULT_BIAS_WARN_PPM = {"1H": 0.05, "13C": 1.0}
DEFAULT_EVIDENCE_WEIGHT = {"COSY": 0.08, "HSQC": 0.20, "HMBC": 0.05}


class Rules:
    def __init__(self, data: Dict[str, Any], raw: str = ""):
        self.data = data
        self.raw = raw or json.dumps(data, ensure_ascii=False, sort_keys=True)
        self.version = str(data.get("version", "rules-unversioned"))

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.data, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]

    def tolerance(self, nucleus: str) -> float:
        return float(self.data.get("tolerance_ppm",
                                   DEFAULT_TOLERANCE_PPM).get(nucleus, 0.2))

    def resolution(self, nucleus: str) -> float:
        return float(self.data.get("overlap_resolution_ppm",
                                   DEFAULT_OVERLAP_RESOLUTION_PPM)
                     .get(nucleus, 0.03))

    def bias_warn(self, nucleus: str) -> float:
        return float(self.data.get("bias_warn_ppm",
                                   DEFAULT_BIAS_WARN_PPM).get(nucleus, 0.05))

    def evidence_weight(self, kind: str) -> float:
        return float(self.data.get("evidence_weight",
                                   DEFAULT_EVIDENCE_WEIGHT).get(kind, 0.0))

    @property
    def missing_edge_penalty(self) -> float:
        return float(self.data.get("missing_edge_penalty", 0.03))

    @property
    def multiplicity_penalty(self) -> float:
        return float(self.data.get("multiplicity_mismatch_penalty", 0.05))

    @property
    def intensity_weight(self) -> float:
        return float(self.data.get("intensity_weight", 0.05))

    def predicted_shift(self, site_id: str, nucleus: str) -> Optional[float]:
        table = self.data.get("predicted_shifts", {}).get(site_id)
        if table is None:
            return None
        value = table.get(nucleus)
        return float(value) if value is not None else None


def parse_rules(text: str) -> Rules:
    data = json.loads(text)
    return Rules(data, raw=text)


def default_rules_text(skeleton: Optional[Dict[str, Any]] = None) -> str:
    predicted: Dict[str, Dict[str, float]] = {}
    if skeleton:
        for spin in skeleton.get("spins", []):
            if spin["nucleus"] == "1H":
                predicted[spin["id"]] = {"1H": 0.0}
    data = {
        "version": "rules-1.0.0",
        "tolerance_ppm": DEFAULT_TOLERANCE_PPM,
        "overlap_resolution_ppm": DEFAULT_OVERLAP_RESOLUTION_PPM,
        "bias_warn_ppm": DEFAULT_BIAS_WARN_PPM,
        "evidence_weight": DEFAULT_EVIDENCE_WEIGHT,
        "missing_edge_penalty": 0.03,
        "multiplicity_mismatch_penalty": 0.05,
        "intensity_weight": 0.05,
        "predicted_shifts": predicted,
    }
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
