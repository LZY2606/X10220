import hashlib
import json


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_id(kind, payload):
    digest = hashlib.sha1(canonical(payload).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (kind, digest)
