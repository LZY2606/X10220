"""Stable, content-addressed identifiers.

Identifiers never depend on wall-clock time, row order insertion counters or
absolute file paths, so a delete-database -> export/import round trip preserves
the same stable identity for the same content.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PREFIX_PROJECT = "prj"
PREFIX_EVENT = "evt"
PREFIX_SOURCE = "src"
PREFIX_PEAK = "pk"
PREFIX_EDGE = "edg"
PREFIX_CANDIDATE = "cnd"
PREFIX_EVIDENCE = "evi"
PREFIX_DECISION = "dcn"
PREFIX_CONFLICT = "cfl"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_id(prefix: str, *parts: Any) -> str:
    """Return ``<prefix>_<16 hex>`` derived deterministically from content parts."""
    payload = _canonical(list(parts))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return "%s_%s" % (prefix, digest)


def fingerprint(value: Any) -> str:
    """40-char parameter fingerprint over canonical JSON."""
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
