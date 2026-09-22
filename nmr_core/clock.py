"""Virtual clock: tests pin time, production uses real UTC."""
from datetime import datetime, timezone

_fixed = None


def set_now(iso_ts):
    global _fixed
    _fixed = iso_ts


def reset():
    global _fixed
    _fixed = None


def now():
    if _fixed is not None:
        return _fixed
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
