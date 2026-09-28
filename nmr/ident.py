"""Stable identity, canonical serialization and fingerprint helpers.

Identities are content-derived so an export -> delete -> import cycle keeps
every id, ordering relation and provenance link byte-identical.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

ALGORITHM_VERSION = "nmr-engine-1.0.0"
PARSER_VERSION = "nmr-parser-1.0.0"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_sha(content: str) -> str:
    return sha256_text(content.replace("\r\n", "\n"))


def fingerprint(obj: Any, *, length: int = 12) -> str:
    return sha256_text(canonical_json(obj))[:length]


def stable_id(prefix: str, *parts: Any, length: int = 10) -> str:
    return f"{prefix}_{fingerprint(parts, length=length)}"


def event_id(kind: str, payload: dict, prev_event_id: str | None) -> str:
    material = {"kind": kind, "payload": payload, "prev": prev_event_id}
    return "evt_" + fingerprint(material, length=16)
