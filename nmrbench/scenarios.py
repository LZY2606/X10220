"""Scenario definitions for the offline NMR counter-evidence archive.

Each scenario starts from the untouched baseline case pack and modifies exactly
one direct source.  The runner asserts that impact is confined to the expected
scope and that accepted as well as rejected/unknown results are both retained.
"""
from __future__ import annotations

import copy
from typing import Callable, Dict

CASE_FILES = {
    "S1H": "1h_ethylbenzene.txt",
    "S13C": "13c_ethylbenzene.txt",
    "COSY": "cosy.txt",
    "HSQC": "hsqc.txt",
    "HMBC": "hmbc.txt",
    "SKEL": "skeleton_ethylbenzene.txt",
    "RULES": "rules_v1.txt",
}


def _replace_line(text: str, prefix: str, new_line: str) -> str:
    out = []
    replaced = False
    for line in text.splitlines():
        if line.startswith(prefix):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        raise AssertionError(f"line with prefix {prefix!r} not found")
    return "\n".join(out) + "\n"


def scenario_overlap(base: Dict[str, str]) -> Dict[str, str]:
    """Move the para 1H peak directly on top of the ortho peak."""
    nxt = dict(base)
    nxt["S1H"] = _replace_line(base["S1H"], "PEAK h4,",
                               "PEAK h4, 7.20ppm, 0.28 a.u., 1 count, m")
    return nxt


def scenario_symmetry(base: Dict[str, str]) -> Dict[str, str]:
    """A skeleton-only revision documents the ortho equivalence class size."""
    nxt = dict(base)
    nxt["SKEL"] = _replace_line(
        base["SKEL"], "# symmetry:",
        "# symmetry: ortho confirmed degenerate (C4 environments 2,6; H2 2,6)")
    return nxt


def scenario_missing_edge(base: Dict[str, str]) -> Dict[str, str]:
    """Delete one HSQC edge (H1-C1) to exercise missing-edge weak evidence."""
    nxt = dict(base)
    lines = [l for l in base["HSQC"].splitlines() if not l.startswith("EDGE q1,")]
    nxt["HSQC"] = "\n".join(lines) + "\n"
    return nxt


def scenario_bias(base: Dict[str, str]) -> Dict[str, str]:
    """Apply a coherent +0.12 ppm offset to every 1H shift."""
    nxt = dict(base)
    lines = []
    for line in base["S1H"].splitlines():
        if line.startswith("PEAK "):
            head, rest = line.split(",", 1)
            cells = [c.strip() for c in rest.split(",")]
            val = float(cells[0].replace("ppm", "")) + 0.12
            cells[0] = f"{val:.2f}ppm"
            lines.append(", ".join([head] + cells))
        else:
            lines.append(line)
    nxt["S1H"] = "\n".join(lines) + "\n"
    return nxt


def scenario_tie(base: Dict[str, str]) -> Dict[str, str]:
    """Loosen the 1H shift window so two aromatic peaks are legal for H2.

    Combined with a tie-breaker-disabled parameter revision, the top two
    candidates must remain tied rather than being arbitrarily forced.
    """
    nxt = dict(base)
    rules = base["RULES"]
    rules = _replace_line(rules, "PARAM shift_window_ppm",
                          "PARAM shift_window_ppm, 0.20")
    rules = _replace_line(rules, "PARAM tie_eps",
                          "PARAM tie_eps, 0.30")
    nxt["RULES"] = rules
    return nxt


def scenario_solvent(base: Dict[str, str]) -> Dict[str, str]:
    """Solvent/temperature/reference meta change only; peaks untouched.

    Only candidates sourced from this 1D file expire; 13C results stay current.
    """
    nxt = dict(base)
    nxt["S1H"] = _replace_line(base["S1H"], "# solvent:",
                               "# solvent: DMSO-d6")
    return nxt


SCENARIOS = {
    "overlap": {"fn": scenario_overlap, "changed_source": "S1H",
                "expect_codes": ["OVERLAP_NOT_FORCED"],
                "scope": "local",
                "description": "coincident peaks must not be force-matched uniquely"},
    "symmetry": {"fn": scenario_symmetry, "changed_source": "SKEL",
                 "expect_codes": ["SYMMETRY_EQUIVALENCE_PRESERVED"],
                 "scope": "local",
                 "description": "symmetry-equivalent nuclei stay co-eligible"},
    "missing_edge": {"fn": scenario_missing_edge, "changed_source": "HSQC",
                     "expect_codes": ["MISSING_EXPECTED_HSQC"],
                     "scope": "local",
                     "description": "absent correlation is weak evidence, not a veto"},
    "global_bias": {"fn": scenario_bias, "changed_source": "S1H",
                    "expect_codes": ["GLOBAL_SHIFT_BIAS"],
                    "scope": "local",
                    "description": "coherent shift offset is measured and flagged"},
    "tied_best": {"fn": scenario_tie, "changed_source": "RULES",
                  "expect_codes": ["TIED_BEST_CANDIDATES"],
                  "scope": "global",
                  "description": "tied top candidates are all retained"},
    "solvent_meta": {"fn": scenario_solvent, "changed_source": "S1H",
                     "expect_codes": [],
                     "scope": "local",
                     "description": "solvent change expires only related candidates"},
}
