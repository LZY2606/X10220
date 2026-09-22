"""Deterministic stable identities for every derived object."""
import hashlib
import json


def digest(*parts):
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stable_id(prefix, *parts, n=12):
    return f"{prefix}-{digest(*parts)[:n]}"
