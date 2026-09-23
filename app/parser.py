"""Parsing and light normalization for immutable NMR source bundles."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

ALLOWED_DOC_KINDS = {
    "1d_spectrum",
    "2d_correlation",
    "molecular_skeleton",
}
ALLOWED_MULTIPLICITIES = {"s", "d", "t", "q", "m", "dd", "dt", "td"}
NUCLEI = {"1H", "13C"}


class BundleFormatError(ValueError):
    def __init__(self, message: str, path: str = "$", fragment: Any = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.fragment = fragment

    def to_dict(self) -> Dict[str, Any]:
        return {"code": "bundle_format_error", "message": self.message, "path": self.path, "fragment": self.fragment}


def _q(raw: Any, *, required: bool, path: str, default_unit: Optional[str] = None) -> Dict[str, Any]:
    unit = default_unit
    text: Any
    if isinstance(raw, dict):
        text = raw.get("raw", raw.get("value"))
        unit = raw.get("unit", unit)
    else:
        text = raw
    if text is None:
        if required:
            raise BundleFormatError("required quantity is missing", path, raw)
        return {"raw": raw, "value": None, "unit": unit, "diagnostic": "missing_quantity"}
    try:
        value = float(text)
    except (TypeError, ValueError):
        return {
            "raw": raw if not isinstance(raw, dict) else raw,
            "value": None,
            "unit": unit or "arbitrary",
            "diagnostic": "unconvertible_quantity",
        }
    return {"raw": raw if not isinstance(raw, dict) else raw, "value": value, "unit": unit}


def _require_object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise BundleFormatError("expected an object", path, value)
    return value


def _require_array(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise BundleFormatError("expected an array", path, value)
    return value


def _require_str(value: Any, path: str, allowed: Optional[set] = None) -> str:
    if not isinstance(value, str) or not value:
        raise BundleFormatError("expected a non-empty string", path, value)
    if allowed is not None and value not in allowed:
        raise BundleFormatError(f"expected one of {sorted(allowed)}", path, value)
    return value


def _checksum(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_bundle(raw_text: str) -> Dict[str, Any]:
    """Parse a copyable JSON project bundle without discarding original text."""
    try:
        bundle = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"invalid JSON: {exc.msg}", f"$.{exc.line}:{exc.col}", raw_text[:160])
    if not isinstance(bundle, dict):
        raise BundleFormatError("bundle root must be an object", "$", bundle)
    project = _require_object(bundle.get("project"), "$.project")
    project_id = _require_str(project.get("id"), "$.project.id")
    name = _require_str(project.get("name"), "$.project.name")
    docs_raw = _require_array(bundle.get("documents"), "$.documents")
    documents: List[Dict[str, Any]] = []
    seen_doc_ids = set()

    for i, doc in enumerate(docs_raw):
        dpath = f"$.documents[{i}]"
        doc_obj = _require_object(doc, dpath)
        doc_id = _require_str(doc_obj.get("id"), f"{dpath}.id")
        if doc_id in seen_doc_ids:
            raise BundleFormatError("duplicate document id", f"{dpath}.id", doc_id)
        seen_doc_ids.add(doc_id)
        kind = _require_str(doc_obj.get("kind"), f"{dpath}.kind", ALLOWED_DOC_KINDS)
        file_name = _require_str(doc_obj.get("file_name"), f"{dpath}.file_name")
        doc_text = doc_obj.get("raw_text")
        if not isinstance(doc_text, str) or not doc_text.strip():
            raise BundleFormatError("raw_text must be a non-empty string", f"{dpath}.raw_text")
        try:
            parsed = json.loads(doc_text)
        except json.JSONDecodeError as exc:
            raise BundleFormatError(
                f"invalid document JSON: {exc.msg}", f"{dpath}.raw_text.{exc.line}:{exc.col}", doc_text[:160]
            )
        normalized = _parse_document(kind, parsed, f"{dpath}.raw_text")
        documents.append(
            {
                "id": doc_id,
                "kind": kind,
                "file_name": file_name,
                "media_type": doc_obj.get("media_type", "application/json"),
                "raw_text": doc_text,
                "checksum": doc_obj.get("checksum") or _checksum(doc_text),
                "parsed": normalized,
            }
        )

    _validate_references(documents)
    return {
        "format": "nmr-candidate-workbench.bundle/v1",
        "project": {"id": project_id, "name": name, "metadata": project.get("metadata", {})},
        "documents": documents,
    }


def _parse_document(kind: str, parsed: Dict[str, Any], path: str) -> Dict[str, Any]:
    root = _require_object(parsed, path)
    if kind == "1d_spectrum":
        return _parse_spectrum(root, path)
    if kind == "2d_correlation":
        return _parse_correlation(root, path)
    return _parse_skeleton(root, path)


def _conditions(root: Dict[str, Any], path: str) -> Dict[str, Any]:
    cond = _require_object(root.get("conditions"), f"{path}.conditions")
    temp = cond.get("temperature")
    return {
        "solvent": _require_str(_require_object(cond.get("solvent"), f"{path}.conditions.solvent").get("name"), f"{path}.conditions.solvent.name"),
        "solvent_raw": cond.get("solvent"),
        "temperature": _q(temp, required=False, path=f"{path}.conditions.temperature", default_unit="K"),
        "reference": _require_object(cond.get("reference"), f"{path}.conditions.reference"),
    }


def _parse_spectrum(root: Dict[str, Any], path: str) -> Dict[str, Any]:
    nucleus = _require_str(root.get("nucleus"), f"{path}.nucleus", NUCLEI)
    frequency = _q(root.get("frequency"), required=True, path=f"{path}.frequency", default_unit="MHz")
    peaks_raw = _require_array(root.get("peaks"), f"{path}.peaks")
    peaks = []
    seen = set()
    for i, peak_raw in enumerate(peaks_raw):
        ppath = f"{path}.peaks[{i}]"
        peak = _require_object(peak_raw, ppath)
        pid = _require_str(peak.get("id"), f"{ppath}.id")
        if pid in seen:
            raise BundleFormatError("duplicate peak id", f"{ppath}.id", pid)
        seen.add(pid)
        mult_raw = peak.get("multiplicity")
        mult_code = mult_raw if isinstance(mult_raw, str) else mult_raw.get("code") if isinstance(mult_raw, dict) else None
        multiplicity = {
            "raw": mult_raw,
            "code": mult_code if mult_code in ALLOWED_MULTIPLICITIES else None,
        }
        if mult_raw is not None and multiplicity["code"] is None:
            multiplicity["diagnostic"] = "unknown_multiplicity"
        peaks.append(
            {
                "id": pid,
                "shift": _q(peak.get("shift"), required=True, path=f"{ppath}.shift", default_unit="ppm"),
                "intensity": _q(peak.get("intensity"), required=False, path=f"{ppath}.intensity"),
                "integral": _q(peak.get("integral"), required=False, path=f"{ppath}.integral"),
                "multiplicity": multiplicity,
                "overlaps_with": peak.get("overlaps_with", []),
                "notes": peak.get("notes"),
                "raw": peak_raw,
            }
        )
    return {"nucleus": nucleus, "frequency": frequency, "conditions": _conditions(root, path), "peaks": peaks}


def _parse_correlation(root: Dict[str, Any], path: str) -> Dict[str, Any]:
    experiment = _require_str(root.get("experiment"), f"{path}.experiment", {"COSY", "HSQC", "HMBC"})
    nuclei = _require_array(root.get("nuclei"), f"{path}.nuclei")
    if len(nuclei) != 2 or any(n not in NUCLEI for n in nuclei):
        raise BundleFormatError("nuclei must contain exactly two supported nuclei", f"{path}.nuclei", nuclei)
    edges_raw = _require_array(root.get("correlations"), f"{path}.correlations")
    edges = []
    seen = set()
    for i, edge_raw in enumerate(edges_raw):
        epath = f"{path}.correlations[{i}]"
        edge = _require_object(edge_raw, epath)
        eid = _require_str(edge.get("id"), f"{epath}.id")
        if eid in seen:
            raise BundleFormatError("duplicate correlation id", f"{epath}.id", eid)
        seen.add(eid)
        edges.append(
            {
                "id": eid,
                "from_peak": _require_str(edge.get("from_peak"), f"{epath}.from_peak"),
                "to_peak": _require_str(edge.get("to_peak"), f"{epath}.to_peak"),
                "strength": _q(edge.get("strength"), required=False, path=f"{epath}.strength"),
                "coordinates": edge.get("coordinates", {}),
                "raw": edge_raw,
            }
        )
    return {
        "experiment": experiment,
        "nuclei": nuclei,
        "frequency_1": _q(root.get("frequency_1"), required=True, path=f"{path}.frequency_1", default_unit="MHz"),
        "frequency_2": _q(root.get("frequency_2"), required=False, path=f"{path}.frequency_2", default_unit="MHz"),
        "conditions": _conditions(root, path),
        "correlations": edges,
    }


def _parse_skeleton(root: Dict[str, Any], path: str) -> Dict[str, Any]:
    nuclei_raw = _require_array(root.get("nuclei"), f"{path}.nuclei")
    nuclei = []
    seen = set()
    for i, nraw in enumerate(nuclei_raw):
        npath = f"{path}.nuclei[{i}]"
        nucleus = _require_object(nraw, npath)
        nid = _require_str(nucleus.get("id"), f"{npath}.id")
        if nid in seen:
            raise BundleFormatError("duplicate nucleus id", f"{npath}.id", nid)
        seen.add(nid)
        nuclei.append(
            {
                "id": nid,
                "label": _require_str(nucleus.get("label"), f"{npath}.label"),
                "element": _require_str(nucleus.get("element"), f"{npath}.element"),
                "observed_nucleus": _require_str(nucleus.get("observed_nucleus"), f"{npath}.observed_nucleus", NUCLEI),
                "atom_class": _require_str(nucleus.get("atom_class"), f"{npath}.atom_class"),
                "symmetry_group": nucleus.get("symmetry_group"),
                "prior_shift": _q(nucleus.get("prior_shift"), required=True, path=f"{npath}.prior_shift", default_unit="ppm"),
                "expected_multiplicity": nucleus.get("expected_multiplicity"),
                "svg": nucleus.get("svg", {}),
                "raw": nucleus,
            }
        )
    bonds_raw = _require_array(root.get("bonds"), f"{path}.bonds")
    bonds = []
    for i, braw in enumerate(bonds_raw):
        bpath = f"{path}.bonds[{i}]"
        bond = _require_object(braw, bpath)
        bonds.append(
            {
                "id": _require_str(bond.get("id"), f"{bpath}.id"),
                "from": _require_str(bond.get("from"), f"{bpath}.from"),
                "to": _require_str(bond.get("to"), f"{bpath}.to"),
                "order": bond.get("order", 1),
                "evidence": bond.get("evidence", "skeleton"),
            }
        )
    return {"nuclei": nuclei, "bonds": bonds}


def _validate_references(documents: List[Dict[str, Any]]) -> None:
    spectra = [d["parsed"] for d in documents if d["kind"] == "1d_spectrum"]
    peak_owner = {peak["id"]: spec["nucleus"] for spec in spectra for peak in spec["peaks"]}
    for doc in documents:
        if doc["kind"] == "2d_correlation":
            parsed = doc["parsed"]
            expected = set(parsed["nuclei"])
            for edge in parsed["correlations"]:
                for endpoint in ("from_peak", "to_peak"):
                    pid = edge[endpoint]
                    if pid not in peak_owner:
                        raise BundleFormatError("correlation references missing peak", f"document {doc['id']} edge {edge['id']}", pid)
                observed = {peak_owner[edge["from_peak"]], peak_owner[edge["to_peak"]]}
                if observed != expected:
                    raise BundleFormatError(
                        "correlation endpoint nuclei do not match declared spectrum",
                        f"document {doc['id']} edge {edge['id']}",
                        [edge["from_peak"], edge["to_peak"]],
                    )
        if doc["kind"] == "molecular_skeleton":
            ids = {n["id"] for n in doc["parsed"]["nuclei"]}
            for bond in doc["parsed"]["bonds"]:
                if bond["from"] not in ids or bond["to"] not in ids:
                    raise BundleFormatError("bond references missing nucleus", f"document {doc['id']} bond {bond['id']}")
