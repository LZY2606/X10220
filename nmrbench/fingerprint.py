"""Deterministic content fingerprints and stable identifiers."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Stable JSON text: sorted keys, no whitespace surprises, non-ASCII preserved."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_short(obj: Any, prefix: str = "") -> str:
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}" if prefix else digest[:16]


def param_fingerprint(params: dict) -> str:
    """Fingerprint of a parameter dictionary (versioned algorithm namespace)."""
    return "fp_" + sha256_short(params)
