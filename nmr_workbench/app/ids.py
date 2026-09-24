"""Stable identity helpers.

Every domain object carries a stable id that is either supplied by the source
file (authoritative, must be preserved verbatim) or deterministically derived
from canonical content.  Content fingerprints are canonical JSON hashes so that
equivalent material always maps to the same fingerprint across export/reimport.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_ID_PREFIX = {
    "project": "proj",
    "source": "src",
    "params": "params",
    "run": "run",
    "candidate": "cand",
    "decision": "dec",
    "branch": "br",
    "event": "ev",
    "fragment": "frag",
}


def content_fingerprint(payload: Any) -> str:
    """Return a stable 16-char fingerprint for any JSON-able payload."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def stable_id(kind: str, *parts: Any) -> str:
    """Build a deterministic id ``<prefix>-<12 char hash>`` from parts."""
    prefix = _ID_PREFIX[kind]
    material = json.dumps(list(parts), ensure_ascii=True, sort_keys=True,
                        separators=(",", ":"), default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def event_id(seq: int, branch_seq: int = 0) -> str:
    """Human- and machine-readable stable event id."""
    return f"ev-{seq:05d}" if branch_seq == 0 else f"ev-{seq:05d}-{branch_seq:03d}"
