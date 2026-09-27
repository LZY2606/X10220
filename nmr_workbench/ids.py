import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}:{sha256_text(canonical_json(list(parts)))[:16]}"
