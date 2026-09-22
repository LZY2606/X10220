from __future__ import annotations

import hashlib
import json


def fingerprint(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]
