"""Deterministic 2D layout helpers for peak/skeleton SVG rendering."""
from __future__ import annotations

from typing import Dict, List, Optional


def skeleton_shapes(parsed_skeleton) -> dict:
    atoms = []
    if parsed_skeleton is None:
        return {"atoms": [], "bonds": []}
    for a in parsed_skeleton.atoms:
        atoms.append({"id": a.label, "x": a.coord[0], "y": a.coord[1],
                      "isotope": a.isotope, "symmetry_class": a.symmetry_class,
                      "equiv_size": a.equiv_size})
    bonds = [{"a": b.a, "b": b.b, "order": b.order}
             for b in parsed_skeleton.bonds]
    xs = [a["x"] for a in atoms] or [0, 1]
    ys = [a["y"] for a in atoms] or [0, 1]
    return {"atoms": atoms, "bonds": bonds,
            "bounds": [min(xs), min(ys), max(xs), max(ys)]}


def peak_axis(peaks: List[dict]) -> List[dict]:
    out = []
    for p in peaks:
        out.append({"id": p["id"], "pid": p["pid"],
                    "shift_ppm": p["shift"]["value"],
                    "intensity": p["intensity"]["value"],
                    "integral": p["integral"]["value"],
                    "multiplicity": p["multiplicity"]})
    return out
