"""Loader for the offline case package 'NMR 共振经纬'."""
import json
import os

CASEPACK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "casepack")


def _read(*parts):
    with open(os.path.join(CASEPACK_DIR, *parts), encoding="utf-8") as fh:
        return fh.read()


def load_compound():
    return json.loads(_read("compound.json"))


def baseline_files():
    return {
        "peaks-1h": _read("baseline", "peaks_1h.txt"),
        "peaks-13c": _read("baseline", "peaks_13c.txt"),
        "peaks-2d": _read("baseline", "peaks_2d.txt"),
    }


def list_scenarios():
    out = []
    cdir = os.path.join(CASEPACK_DIR, "counterexamples")
    for name in sorted(os.listdir(cdir)):
        if name.endswith(".json"):
            with open(os.path.join(cdir, name), encoding="utf-8") as fh:
                spec = json.load(fh)
            out.append({"name": spec["name"], "title": spec["title"],
                        "description": spec["description"]})
    return out


def load_scenario(name):
    path = os.path.join(CASEPACK_DIR, "counterexamples", name + ".json")
    if not os.path.exists(path):
        raise KeyError("unknown scenario %s" % name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
