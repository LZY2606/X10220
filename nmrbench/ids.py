"""Stable identities, canonical JSON, deterministic clocks, fixed seeds."""
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone, tzinfo

SCHEMA_VERSION = "1.0.0"
DEFAULT_SEED = 20260924


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def content_id(prefix: str, obj) -> str:
    return f"{prefix}-{sha1_hex(canonical(obj))[:16]}"


def doc_id(kind: str, raw_text: str) -> str:
    return f"doc-{sha1_hex(kind + chr(0) + raw_text)[:16]}"


def event_id(seq: int, branch_id: str, payload) -> str:
    return f"evt-{sha1_hex(f'{seq}|{branch_id}|' + canonical(payload))[:16]}"


def params_fingerprint(params: dict) -> str:
    return "pfp-" + sha1_hex(canonical(params))[:16]


def candidate_id(set_id: str, nucleus_id: str, peak_id: str) -> str:
    return "cand-" + sha1_hex(f"{set_id}|{nucleus_id}|{peak_id}")[:16]


def decision_id(branch_id: str, nucleus_id: str, decided_at: str) -> str:
    return "dec-" + sha1_hex(f"{branch_id}|{nucleus_id}|{decided_at}")[:16]


class Clock:
    """Virtual, scriptable clock. No wall-clock reads anywhere in the app."""

    def __init__(self, epoch: str = "2026-09-24T00:00:00+09:00"):
        self._epoch = datetime.fromisoformat(epoch)
        self._n = 0

    def tick(self) -> str:
        ts = self._epoch + timedelta(seconds=self._n)
        self._n += 1
        return ts.isoformat()


def make_rng(seed: int = DEFAULT_SEED) -> random.Random:
    return random.Random(seed)
