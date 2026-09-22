"""Input format validation, entity diffs and default parameters.

The import layer accepts well-formed documents and rejects clear format
errors.  Values that merely cannot be normalized (e.g. an unknown unit)
are still imported, kept verbatim, and diagnosed in the derived layer.
"""
from __future__ import annotations

from .util import canonical, short_hash

CHANNELS = ("1h", "13c")
CORRELATION_TYPES = ("COSY", "HSQC", "HMBC")
FORMATS = ("molecule", "peaks_1d", "correlations")

DEFAULT_PARAMS = {
    "solvent": "CDCl3",
    "temperature_k": 298.0,
    "frequency_mhz_1h": 400.0,
    "frequency_mhz_13c": 100.0,
    "reference_standard_1h": "TMS",
    "reference_standard_13c": "TMS",
    "reference_offset_1h": 0.0,
    "reference_offset_13c": 0.0,
    "tolerance_ppm_1h": 0.15,
    "tolerance_ppm_13c": 1.0,
    "overlap_tolerance_ppm": 0.03,
}

_NUMERIC_PARAM_KEYS = (
    "temperature_k",
    "frequency_mhz_",
    "reference_offset_",
    "tolerance_ppm_",
    "overlap_tolerance_ppm",
)


def param_fingerprint(params) -> str:
    return short_hash(canonical(params))


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_params(params):
    diagnostics = []
    if not isinstance(params, dict):
        return ["params must be a JSON object"]
    for key, value in params.items():
        if not isinstance(key, str):
            diagnostics.append("param key %r is not a string" % (key,))
        elif key.startswith(_NUMERIC_PARAM_KEYS) and not _is_number(value):
            diagnostics.append("param %s must be numeric, got %r" % (key, value))
    return diagnostics


def validate(fmt, content):
    if fmt == "peaks_1d":
        return _validate_peaks(content)
    if fmt == "correlations":
        return _validate_correlations(content)
    if fmt == "molecule":
        return _validate_molecule(content)
    return ["unknown input format %r" % (fmt,)]


def _validate_peaks(content):
    diagnostics = []
    if not isinstance(content, dict):
        return ["peaks_1d content must be a JSON object"]
    if content.get("kind") != "peaks_1d":
        diagnostics.append("peaks_1d content requires kind == 'peaks_1d'")
    if content.get("channel") not in CHANNELS:
        diagnostics.append("peaks_1d channel must be one of %s" % (CHANNELS,))
    peaks = content.get("peaks")
    if not isinstance(peaks, list) or not peaks:
        diagnostics.append("peaks_1d requires a non-empty 'peaks' list")
        return diagnostics
    seen = set()
    for index, peak in enumerate(peaks):
        where = "peaks[%d]" % index
        if not isinstance(peak, dict):
            diagnostics.append("%s must be an object" % where)
            continue
        peak_id = peak.get("id")
        if not isinstance(peak_id, str) or not peak_id:
            diagnostics.append("%s.id must be a non-empty string" % where)
        elif peak_id in seen:
            diagnostics.append("%s.id %r is duplicated" % (where, peak_id))
        else:
            seen.add(peak_id)
        if not _is_number(peak.get("shift")):
            diagnostics.append("%s.shift must be numeric, got %r" % (where, peak.get("shift")))
        if not isinstance(peak.get("shift_unit"), str):
            diagnostics.append("%s.shift_unit must be a string" % where)
        for field in ("intensity", "integral"):
            if field in peak and peak[field] is not None and not _is_number(peak[field]):
                diagnostics.append("%s.%s must be numeric, got %r" % (where, field, peak[field]))
        if "multiplicity" in peak and peak["multiplicity"] is not None \
                and not isinstance(peak["multiplicity"], str):
            diagnostics.append("%s.multiplicity must be a string" % where)
    return diagnostics


def _validate_correlations(content):
    diagnostics = []
    if not isinstance(content, dict):
        return ["correlations content must be a JSON object"]
    if content.get("kind") != "correlations":
        diagnostics.append("correlations content requires kind == 'correlations'")
    edges = content.get("edges")
    if not isinstance(edges, list) or not edges:
        diagnostics.append("correlations requires a non-empty 'edges' list")
        return diagnostics
    seen = set()
    for index, edge in enumerate(edges):
        where = "edges[%d]" % index
        if not isinstance(edge, dict):
            diagnostics.append("%s must be an object" % where)
            continue
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id:
            diagnostics.append("%s.id must be a non-empty string" % where)
        elif edge_id in seen:
            diagnostics.append("%s.id %r is duplicated" % (where, edge_id))
        else:
            seen.add(edge_id)
        if edge.get("type") not in CORRELATION_TYPES:
            diagnostics.append("%s.type must be one of %s" % (where, CORRELATION_TYPES))
        for endpoint in ("a", "b"):
            if not isinstance(edge.get(endpoint), str) or not edge.get(endpoint):
                diagnostics.append("%s.%s must be a non-empty peak id" % (where, endpoint))
    return diagnostics


def _validate_molecule(content):
    diagnostics = []
    if not isinstance(content, dict):
        return ["molecule content must be a JSON object"]
    if content.get("kind") != "molecule":
        diagnostics.append("molecule content requires kind == 'molecule'")
    atoms = content.get("atoms")
    if not isinstance(atoms, list) or not atoms:
        diagnostics.append("molecule requires a non-empty 'atoms' list")
        return diagnostics
    seen = set()
    for index, atom in enumerate(atoms):
        where = "atoms[%d]" % index
        if not isinstance(atom, dict):
            diagnostics.append("%s must be an object" % where)
            continue
        atom_id = atom.get("id")
        if not isinstance(atom_id, str) or not atom_id:
            diagnostics.append("%s.id must be a non-empty string" % where)
        elif atom_id in seen:
            diagnostics.append("%s.id %r is duplicated" % (where, atom_id))
        else:
            seen.add(atom_id)
        if not isinstance(atom.get("element"), str):
            diagnostics.append("%s.element must be a string" % where)
        if not _is_number(atom.get("predicted_ppm")):
            diagnostics.append("%s.predicted_ppm must be numeric" % where)
        if not isinstance(atom.get("neighbors", []), list):
            diagnostics.append("%s.neighbors must be a list" % where)
    for index, bond in enumerate(content.get("bonds", [])):
        if not (isinstance(bond, list) and len(bond) == 2 and all(isinstance(b, str) for b in bond)):
            diagnostics.append("bonds[%d] must be a pair of atom ids" % index)
        elif bond[0] not in seen or bond[1] not in seen:
            diagnostics.append("bonds[%d] references an unknown atom" % index)
    return diagnostics


def diff_entities(fmt, old_content, new_content):
    """Entity-level diff used for local invalidation.

    Returns (changed_peak_ids, changed_edge_ids, changed_atom_ids); removed
    or modified entities count as changed, pure additions do not invalidate
    existing derived rows.  Returns None when the format is unknown, which
    callers treat as 'expire everything derived from this input'.
    """
    if fmt == "peaks_1d":
        old = {p["id"]: p for p in old_content.get("peaks", [])}
        new = {p["id"]: p for p in new_content.get("peaks", [])}
        changed = {i for i in old if i not in new or canonical(old[i]) != canonical(new[i])}
        return changed, set(), set()
    if fmt == "correlations":
        old = {e["id"]: e for e in old_content.get("edges", [])}
        new = {e["id"]: e for e in new_content.get("edges", [])}
        changed = {i for i in old if i not in new or canonical(old[i]) != canonical(new[i])}
        return set(), changed, set()
    if fmt == "molecule":
        old = {a["id"]: a for a in old_content.get("atoms", [])}
        new = {a["id"]: a for a in new_content.get("atoms", [])}
        changed = {i for i in old if i not in new or canonical(old[i]) != canonical(new[i])}
        return set(), set(), changed
    return None
