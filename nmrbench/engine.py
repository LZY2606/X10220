"""Domain engine: candidates, evidence, rejections and diagnostics.

The engine is *pure*: it takes parsed documents + a parameter set and returns
fully versioned, content-addressed results.  It never deletes or overwrites:

* overlapping peaks / missing peaks are never force-matched uniquely;
* the coupling network is only supporting evidence (never a hard reject);
* every candidate carries a score, evidence detail and/or reject reasons;
* ties are preserved ("rank" is shared) instead of an arbitrary winner.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .fingerprint import sha256_short

# Default scoring parameters.  Every run records the exact fingerprint of the
# parameters actually used.
DEFAULT_PARAMS: dict[str, Any] = {
    "shift_tol_ppm": 0.6,          # 1H hard matching window
    "carbon_tol_ppm": 5.0,         # 13C hard matching window
    "hsqc_tol_f2_ppm": 0.08,       # proton dimension
    "hsqc_tol_f1_ppm": 0.6,        # carbon dimension
    "hmbc_tol_f2_ppm": 0.08,
    "hmbc_tol_f1_ppm": 0.8,
    "cosy_tol_ppm": 0.06,
    "shift_weight": 1.0,
    "hsqc_weight": 1.2,
    "hmbc_weight": 1.0,
    "cosy_weight": 0.8,
    "tie_eps": 1e-9,
    "overlap_sep_ppm": 0.03,       # <= this proton peak separation => overlap
    "carbon_overlap_sep_ppm": 0.06,
    "global_bias_alarm_ppm": 0.25,
    "global_bias_atom_count": 3,
    "multiplier": 1.0,
    "offset_ppm": 0.0,             # reference/bias correction term
}

OH_ELEMENTS = {"O", "N", "S"}


@dataclass
class DocSet:
    molecule: dict | None = None
    docs: dict[str, list[dict]] = None  # kind -> list of parsed-doc dicts

    def latest(self, kind: str) -> dict | None:
        items = (self.docs or {}).get(kind) or []
        return items[-1] if items else None


# ---------------------------------------------------------------------------
# Molecular model
# ---------------------------------------------------------------------------
def build_model(molecule: dict | None) -> dict[str, Any]:
    if molecule is None:
        return {"atoms": [], "sites": [], "bonds": [], "adj": {}, "by_id": {}}
    atom_rows = [r for r in molecule["rows"] if r["kind"] == "atom"]
    bond_rows = [r for r in molecule["rows"] if r["kind"] == "bond"]

    atoms = []
    by_id = {}
    for r in atom_rows:
        shift = r["shift_hint"]
        atom = {
            "atom_id": r["atom_id"], "element": r["element"],
            "hydrogens": r["hydrogens"], "equiv_of": r["equiv_of"],
            "tag": r.get("tag", ""), "row_id": r["row_id"],
            "shift_hint_raw": shift["raw"],
            "shift_hint_value": shift["value"],
            "shift_hint_diagnostic": shift["diagnostic"],
        }
        atoms.append(atom)
        by_id[atom["atom_id"]] = atom

    adj: dict[str, set[str]] = defaultdict(set)
    bonds = []
    for r in bond_rows:
        adj[r["a"]].add(r["b"])
        adj[r["b"]].add(r["a"])
        bonds.append({"a": r["a"], "b": r["b"], "order": r["order"],
                      "row_id": r["row_id"]})

    # Sites: equivalence leader atom, plus explicit H hydrogens.
    sites = []
    for atom in atoms:
        if atom["equiv_of"] is not None:
            continue
        members = [atom["atom_id"]] + [
            a["atom_id"] for a in atoms if a["equiv_of"] == atom["atom_id"]]
        if atom["element"] == "C":
            nucleus = "13c"
        elif atom["element"] == "H":
            nucleus = "1h"
        elif atom["element"] in OH_ELEMENTS and atom["hydrogens"] > 0:
            nucleus = "1h"
        else:
            continue
        sites.append({
            "site_id": f"site-{atom['atom_id']}",
            "leader": atom["atom_id"], "nucleus": nucleus,
            "atom_ids": members, "element": atom["element"],
            "expected_h": sum(by_id[m]["hydrogens"] for m in members),
            "shift_hint_value": atom["shift_hint_value"],
            "shift_hint_raw": atom["shift_hint_raw"],
            "has_hint": atom["shift_hint_value"] is not None,
            "hint_diagnostic": atom["shift_hint_diagnostic"],
            "symmetry": len(members) > 1,
        })
    return {"atoms": atoms, "sites": sites, "bonds": bonds,
            "adj": {k: sorted(v) for k, v in adj.items()}, "by_id": by_id}


def bond_distance(model: dict, a: str, b: str) -> int | None:
    adj = model["adj"]
    if a not in adj or b not in adj:
        return None
    if a == b:
        return 0
    frontier = [(a, 0)]
    seen = {a}
    while frontier:
        node, dist = frontier.pop(0)
        for nxt in adj.get(node, ()):
            if nxt in seen:
                continue
            if nxt == b:
                return dist + 1
            seen.add(nxt)
            frontier.append((nxt, dist + 1))
    return None
