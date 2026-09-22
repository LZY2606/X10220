"""Content-addressed stable identifiers and parameter fingerprints.

Every long-lived identity is derived from *content*, never from an
insertion counter, so export -> delete -> import preserves identities and
order.  Only lowercase hex prefixes of SHA-256 are used.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

PREFIX = {
    "src": "src",
    "pk": "pk",
    "ed": "ed",
    "st": "st",
    "gr": "gr",
    "ex": "ex",
    "gen": "gen",
    "cand": "cand",
    "evt": "evt",
    "br": "br",
    "dec": "dec",
    "proj": "proj",
}


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def id_of(kind: str, payload: Any) -> str:
    return "%s_%s" % (PREFIX[kind], digest(payload)[:16])


def fingerprint(payload: Any) -> str:
    """Short deterministic parameter / content fingerprint."""
    return "fp_" + digest(payload)[:12]


def ordered(seq: Iterable[Any]) -> list:
    """Return a canonical (sorted) list for set-like payloads."""
    return sorted(seq, key=lambda item: _canonical(item))
