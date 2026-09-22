import copy
import hashlib
import json


class Clock:
    """Virtual-time capable clock. Tests freeze/advance a fixed time; real runs use wall time."""

    def __init__(self, fixed=None):
        self._fixed = fixed

    def now(self):
        if self._fixed is not None:
            return self._fixed
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def advance(self, seconds):
        if self._fixed is None:
            raise RuntimeError("advance only valid on a frozen clock")
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(self._fixed) + timedelta(seconds=seconds)
        self._fixed = dt.isoformat(timespec="microseconds")
        return self._fixed


def stable_id(prefix, parts):
    payload = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return "%s_%s" % (prefix, digest)


def canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def param_fingerprint(params):
    return stable_id("fp", canon(params))


def short_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def deep_copy(obj):
    return copy.deepcopy(obj)
