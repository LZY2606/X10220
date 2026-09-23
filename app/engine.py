from __future__ import annotations

import csv
import hashlib
import io
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .storage import canonical_json


ALGORITHM_VERSION = "nmr-assignment-1.0.0"


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def param_fingerprint(params: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()[:12]


def initial_state() -> dict[str, Any]:
    return {
        "project": {"id": "project-demo", "name": "NMR resonance evidence room", "created_event_id": None},
        "sources": {},
        "source_order": [],
        "peaks": {},
        "edges": {},
        "nuclei": {},
        "bonds": [],
        "conditions": {},
        "runs": {},
        "run_order": [],
        "parameters": {},
        "decisions": [],
        "conflicts": [],
        "branches": {"main": {"branch_id": "main", "label": "main", "parent": None, "fork_event_id": None}},
        "events": [],
        "diagnostics": [],
        "dependencies": {},
    }


def _required(row: dict[str, str], field: str, source_id: str, kind: str, index: int, diagnostics: list[dict[str, Any]]) -> str | None:
    value = row.get(field, "")
    if value is None or str(value).strip() == "":
        diagnostics.append({
            "severity": "error",
            "code": "MISSING_REQUIRED_FIELD",
            "source_id": source_id,
            "record_kind": kind,
            "record_index": index,
            "field": field,
            "raw_value": value,
            "message": f"{kind} record {index} missing {field}",
        })
        return None
    return str(value).strip()


def _quantity(value: str | None, field: str, source_id: str, kind: str, index: int, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"raw": value, "value": None, "unit": None, "convertible": False}
    if value is None:
        return result
    text = value.strip()
    if " " not in text:
        diagnostics.append({
            "severity": "error",
            "code": "MISSING_UNIT_OR_COORDINATE_REFERENCE",
            "source_id": source_id,
            "record_kind": kind,
            "record_index": index,
            "field": field,
            "raw_value": value,
            "message": "domain quantity must retain a unit or coordinate reference",
        })
        return result
    number, unit = text.rsplit(" ", 1)
    try:
        result["value"] = float(number)
        result["unit"] = unit
        result["convertible"] = True
    except ValueError:
        diagnostics.append({
            "severity": "warning",
            "code": "UNCONVERTIBLE_VALUE_RETAINED",
            "source_id": source_id,
            "record_kind": kind,
            "record_index": index,
            "field": field,
            "raw_value": value,
            "message": "value retained at source position and excluded from numeric derivation",
        })
    return result


def _number(value: str | None, source_id: str, kind: str, index: int, field: str, diagnostics: list[dict[str, Any]], default: float | None = None) -> float | None:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip())
    except ValueError:
        diagnostics.append({
            "severity": "warning",
            "code": "UNCONVERTIBLE_VALUE_RETAINED",
            "source_id": source_id,
            "record_kind": kind,
            "record_index": index,
            "field": field,
            "raw_value": value,
            "message": "numeric value retained as raw text and excluded from derivation",
        })
        return default


def _split_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def parse_csv_document(content: str, source_id: str, version: str) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("empty CSV document")
    reader = csv.DictReader(io.StringIO(content))
    required_columns = {"record_type", "id", "isotope_or_nucleus", "raw"}
    if reader.fieldnames is None or not required_columns.issubset(set(reader.fieldnames)):
        raise ValueError(f"CSV header must contain {sorted(required_columns)}")

    peaks: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    nuclei: dict[str, dict[str, Any]] = {}
    bonds: list[dict[str, Any]] = []
    conditions: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    for index, row in enumerate(reader, start=1):
        record_type = (row.get("record_type") or "").strip().lower()
        record_id = _required(row, "id", source_id, record_type, index, diagnostics)
        isotope = _required(row, "isotope_or_nucleus", source_id, record_type, index, diagnostics)
        if not record_id or not isotope:
            continue

        if record_type == "peak":
            shift = _quantity(row.get("shift"), "shift", source_id, record_type, index, diagnostics)
            peaks[record_id] = {
                "id": record_id,
                "peak_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "isotope": isotope,
                "shift": shift,
                "intensity": {"raw": row.get("intensity", ""), "value": _number(row.get("intensity"), source_id, record_type, index, "intensity", diagnostics)},
                "integration": {"raw": row.get("integration", ""), "value": _number(row.get("integration"), source_id, record_type, index, "integration", diagnostics)},
                "multiplicity": {"raw": row.get("multiplicity", ""), "normalized": (row.get("multiplicity") or "").strip() or None},
                "overlap_group": (row.get("overlap_group") or "").strip() or None,
                "condition_id": (row.get("condition_id") or "").strip() or None,
                "raw_text": json.dumps(row, ensure_ascii=False, sort_keys=True),
            }
        elif record_type == "edge":
            edge_type = _required(row, "edge_type", source_id, record_type, index, diagnostics)
            from_peak = _required(row, "from_peak", source_id, record_type, index, diagnostics)
            to_peak = _required(row, "to_peak", source_id, record_type, index, diagnostics)
            if edge_type and from_peak and to_peak:
                edges[record_id] = {
                    "id": record_id,
                    "edge_id": record_id,
                    "source_id": source_id,
                    "source_version": version,
                    "edge_type": edge_type,
                    "from_peak": from_peak,
                    "to_peak": to_peak,
                    "coordinate_basis": row.get("raw", "").strip(),
                    "strength": {"raw": row.get("strength", ""), "value": _number(row.get("strength"), source_id, record_type, index, "strength", diagnostics)},
                    "condition_id": (row.get("condition_id") or "").strip() or None,
                    "raw_text": json.dumps(row, ensure_ascii=False, sort_keys=True),
                }
        elif record_type == "nucleus":
            predicted = _quantity(row.get("predicted_shift"), "predicted_shift", source_id, record_type, index, diagnostics)
            nuclei[record_id] = {
                "id": record_id,
                "nucleus_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "isotope": isotope,
                "label": (row.get("label") or record_id).strip(),
                "predicted_shift": predicted,
                "attached_carbon": (row.get("attached_carbon") or "").strip() or None,
                "equivalence_group": (row.get("equivalence_group") or "").strip() or record_id,
                "group_size": int(_number(row.get("group_size"), source_id, record_type, index, "group_size", diagnostics, 1) or 1),
                "x": _number(row.get("x"), source_id, record_type, index, "x", diagnostics, 0.0),
                "y": _number(row.get("y"), source_id, record_type, index, "y", diagnostics, 0.0),
                "raw_text": json.dumps(row, ensure_ascii=False, sort_keys=True),
            }
        elif record_type == "bond":
            other = _required(row, "bonded_to", source_id, record_type, index, diagnostics)
            if other:
                bonds.append({"source_id": source_id, "source_version": version, "from": record_id, "to": other, "raw_text": json.dumps(row, ensure_ascii=False, sort_keys=True)})
        elif record_type == "condition":
            conditions[record_id] = {
                "id": record_id,
                "condition_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "isotopes": _split_ids(isotope),
                "solvent": (row.get("solvent") or "").strip(),
                "temperature": {"raw": row.get("temperature", ""), "value": _number(row.get("temperature"), source_id, record_type, index, "temperature", diagnostics), "unit": "K" if row.get("temperature") else None},
                "spectrometer_frequency": _quantity(row.get("spectrometer_frequency"), "spectrometer_frequency", source_id, record_type, index, diagnostics),
                "reference_standard": (row.get("reference_standard") or "").strip(),
                "raw_text": json.dumps(row, ensure_ascii=False, sort_keys=True),
            }
        else:
            diagnostics.append({
                "severity": "error",
                "code": "UNKNOWN_RECORD_TYPE",
                "source_id": source_id,
                "record_kind": record_type,
                "record_index": index,
                "field": "record_type",
                "raw_value": record_type,
                "message": "expected peak, edge, nucleus, bond, or condition",
            })

    return {
        "peaks": peaks,
        "edges": edges,
        "nuclei": nuclei,
        "bonds": bonds,
        "conditions": conditions,
        "diagnostics": diagnostics,
    }


def _json_quantity(value: Any, source_id: str, kind: str, record_id: str, field: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    if value is None:
        return {"raw": None, "value": None, "unit": None, "convertible": False}
    if isinstance(value, dict):
        raw = value.get("raw")
        number = value.get("value")
        unit = value.get("unit")
        if isinstance(number, (int, float)) and unit:
            return {"raw": raw if raw is not None else f"{number} {unit}", "value": float(number), "unit": unit, "convertible": True}
        diagnostics.append({
            "severity": "warning",
            "code": "UNCONVERTIBLE_VALUE_RETAINED",
            "source_id": source_id,
            "record_kind": kind,
            "record_id": record_id,
            "field": field,
            "raw_value": raw,
            "message": "structured quantity lacked numeric value/unit and remains at source position",
        })
        return {"raw": raw, "value": None, "unit": unit, "convertible": False}
    if isinstance(value, str) and " " in value:
        num, unit = value.rsplit(" ", 1)
        try:
            return {"raw": value, "value": float(num), "unit": unit, "convertible": True}
        except ValueError:
            pass
    diagnostics.append({
        "severity": "warning",
        "code": "UNCONVERTIBLE_VALUE_RETAINED",
        "source_id": source_id,
        "record_kind": kind,
        "record_id": record_id,
        "field": field,
        "raw_value": value,
        "message": "domain quantity must carry value plus unit or coordinate reference",
    })
    return {"raw": value, "value": None, "unit": None, "convertible": False}


def parse_json_document(content: str, source_id: str, version: str) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("empty JSON document")
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        raise ValueError("JSON document must be an object containing a records array")

    peaks: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    nuclei: dict[str, dict[str, Any]] = {}
    bonds: list[dict[str, Any]] = []
    conditions: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    for index, raw_record in enumerate(document["records"], start=1):
        if not isinstance(raw_record, dict):
            raise ValueError(f"record {index} must be an object")
        record = deepcopy(raw_record)
        record_type = str(record.get("record_type", "")).strip().lower()
        record_id = record.get("id")
        isotope = record.get("isotope_or_nucleus")
        if not record_type or not record_id or not isotope:
            raise ValueError(f"record {index} requires record_type, id, and isotope_or_nucleus")
        record_id = str(record_id)
        if record_type == "peak":
            peaks[record_id] = {
                "id": record_id,
                "peak_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "isotope": str(isotope),
                "shift": _json_quantity(record.get("shift"), source_id, record_type, record_id, "shift", diagnostics),
                "intensity": {"raw": record.get("intensity"), "value": record.get("intensity") if isinstance(record.get("intensity"), (int, float)) else None},
                "integration": {"raw": record.get("integration"), "value": record.get("integration") if isinstance(record.get("integration"), (int, float)) else None},
                "multiplicity": {"raw": record.get("multiplicity"), "normalized": str(record.get("multiplicity")) if record.get("multiplicity") is not None else None},
                "overlap_group": record.get("overlap_group"),
                "condition_id": record.get("condition_id"),
                "raw_text": json.dumps(record, ensure_ascii=False, sort_keys=True),
            }
        elif record_type == "edge":
            for field in ("edge_type", "from_peak", "to_peak"):
                if not record.get(field):
                    raise ValueError(f"edge {record_id} requires {field}")
            edges[record_id] = {
                "id": record_id,
                "edge_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "edge_type": str(record["edge_type"]).upper(),
                "from_peak": str(record["from_peak"]),
                "to_peak": str(record["to_peak"]),
                "coordinate_basis": str(record.get("raw", "observed NMR correlation")),
                "strength": {"raw": record.get("strength"), "value": record.get("strength") if isinstance(record.get("strength"), (int, float)) else None},
                "condition_id": record.get("condition_id"),
                "raw_text": json.dumps(record, ensure_ascii=False, sort_keys=True),
            }
        elif record_type == "nucleus":
            if not isinstance(isotope, str):
                raise ValueError(f"nucleus {record_id} isotope_or_nucleus must be a string")
            nuclei[record_id] = {
                "id": record_id,
                "nucleus_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "isotope": isotope,
                "label": str(record.get("label", record_id)),
                "predicted_shift": _json_quantity(record.get("predicted_shift"), source_id, record_type, record_id, "predicted_shift", diagnostics),
                "attached_carbon": record.get("attached_carbon"),
                "equivalence_group": str(record.get("equivalence_group", record_id)),
                "group_size": int(record.get("group_size", 1)),
                "x": float(record.get("x", 0.0)),
                "y": float(record.get("y", 0.0)),
                "raw_text": json.dumps(record, ensure_ascii=False, sort_keys=True),
            }
        elif record_type == "bond":
            if not record.get("bonded_to"):
                raise ValueError(f"bond {record_id} requires bonded_to")
            bonds.append({"source_id": source_id, "source_version": version, "from": record_id, "to": str(record["bonded_to"]), "raw_text": json.dumps(record, ensure_ascii=False, sort_keys=True)})
        elif record_type == "condition":
            frequency = _json_quantity(record.get("spectrometer_frequency"), source_id, record_type, record_id, "spectrometer_frequency", diagnostics)
            conditions[record_id] = {
                "id": record_id,
                "condition_id": record_id,
                "source_id": source_id,
                "source_version": version,
                "isotopes": record.get("isotopes", [isotope]) if isinstance(record.get("isotopes", isotope), list) else [record.get("isotopes", isotope)],
                "solvent": record.get("solvent"),
                "temperature": {"raw": record.get("temperature"), "value": record.get("temperature") if isinstance(record.get("temperature"), (int, float)) else None, "unit": "K"},
                "spectrometer_frequency": frequency,
                "reference_standard": record.get("reference_standard"),
                "raw_text": json.dumps(record, ensure_ascii=False, sort_keys=True),
            }
        else:
            raise ValueError(f"record {index} has unsupported record_type {record_type}")

    return {"peaks": peaks, "edges": edges, "nuclei": nuclei, "bonds": bonds, "conditions": conditions, "diagnostics": diagnostics}


def parse_document(content: str, source_id: str, version: str, media_type: str = "application/json") -> dict[str, Any]:
    if media_type.endswith("csv") or content.lstrip().startswith("record_type,"):
        return parse_csv_document(content, source_id, version)
    return parse_json_document(content, source_id, version)


@dataclass
class Graph:
    adjacency: dict[str, set[str]]

    def distance(self, start: str, end: str) -> int | None:
        if start == end:
            return 0
        seen = {start}
        frontier = [(start, 0)]
        while frontier:
            node, depth = frontier.pop(0)
            for nxt in sorted(self.adjacency.get(node, set())):
                if nxt in seen:
                    continue
                if nxt == end:
                    return depth + 1
                seen.add(nxt)
                frontier.append((nxt, depth + 1))
        return None


def build_graph(state: dict[str, Any]) -> Graph:
    adjacency: dict[str, set[str]] = {nucleus_id: set() for nucleus_id in state["nuclei"]}
    for bond in state["bonds"]:
        left, right = bond["from"], bond["to"]
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    return Graph(adjacency)


def _bias(params: dict[str, Any], isotope: str) -> tuple[float, float]:
    entry = params.get("global_shift_bias", {}).get(isotope, {})
    value = float(entry.get("value", 0.0))
    uncertainty = float(entry.get("uncertainty", 0.0))
    return value, uncertainty


def _isotope_condition_ids(state: dict[str, Any], isotope: str) -> list[str]:
    result = []
    for condition in state["conditions"].values():
        isotopes = condition.get("isotopes") or []
        if isotope in isotopes or any(str(item).lower() == isotope.lower() for item in isotopes):
            result.append(condition["id"])
    return sorted(result)


def _shift_match(peak: dict[str, Any], nucleus: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    observed = peak["shift"].get("value")
    predicted = nucleus["predicted_shift"].get("value")
    if observed is None:
        return {"within": False, "residual": None, "score": 0.0, "uncertainty": None, "reject_reason": "OBSERVED_SHIFT_NOT_CONVERTIBLE"}
    if predicted is None:
        return {"within": False, "residual": None, "score": 0.0, "uncertainty": None, "reject_reason": "PREDICTED_SHIFT_NOT_CONVERTIBLE"}
    bias, bias_uncertainty = _bias(params, nucleus["isotope"])
    sigma = {"1H": 0.12, "13C": 0.35}.get(nucleus["isotope"], 0.2)
    uncertainty = (sigma**2 + bias_uncertainty**2) ** 0.5
    hard_tolerance = {"1H": 0.65, "13C": 2.0}.get(nucleus["isotope"], 0.8)
    residual = observed - predicted - bias
    score = 100.0 * float(np_exp(-0.5 * (residual / uncertainty) ** 2))
    within = abs(residual) <= hard_tolerance
    return {
        "within": within,
        "residual": round(residual, 6),
        "score": round(score, 6),
        "uncertainty": round(uncertainty, 6),
        "reject_reason": None if within else "SHIFT_OUTSIDE_HARD_TOLERANCE",
    }


def np_exp(value: float) -> float:
    import numpy as np

    return float(np.exp(value))


def _multiplicity_score(peak: dict[str, Any], nucleus: dict[str, Any]) -> float:
    observed = (peak.get("multiplicity", {}).get("normalized") or "").lower()
    predicted = str(nucleus.get("label", "")).lower()
    if not observed:
        return 0.0
    if nucleus["isotope"] != "1H":
        return 0.0
    expected = {
        "h1": "s",
        "h3": "m",
        "h4": "m",
        "h5": "t",
        "oh": "br s",
    }.get("".join(ch for ch in nucleus["id"].lower() if ch.isalpha() or ch.isdigit()).rstrip("ab"), "")
    if observed == expected:
        return 10.0
    if expected and (expected[0] in observed or observed in expected):
        return 5.0
    return 0.0


def _integration_score(peak: dict[str, Any], nucleus: dict[str, Any]) -> tuple[float, str | None]:
    if nucleus["isotope"] != "1H":
        return 0.0, None
    integration = peak.get("integration", {}).get("value")
    if integration is None:
        return 0.0, "INTEGRAL_UNKNOWN"
    group_size = float(nucleus.get("group_size", 1))
    if peak.get("overlap_group"):
        if integration + 0.05 >= group_size:
            return 8.0, None
        return 0.0, "OVERLAP_INTEGRAL_TOO_SMALL"
    if abs(integration - group_size) <= 0.15:
        return 10.0, None
    return max(0.0, 10.0 - abs(integration - group_size) * 4), "INTEGRAL_MISMATCH"


def _observed_edges_for_peak(state: dict[str, Any], peak_id: str) -> list[dict[str, Any]]:
    return [edge for edge in state["edges"].values() if edge["from_peak"] == peak_id or edge["to_peak"] == peak_id]


def _other_end(edge: dict[str, Any], peak_id: str) -> str:
    return edge["to_peak"] if edge["from_peak"] == peak_id else edge["from_peak"]


def _peak_matches_nucleus(state: dict[str, Any], peak_id: str, nucleus_id: str, params: dict[str, Any]) -> bool:
    peak = state["peaks"].get(peak_id)
    nucleus = state["nuclei"].get(nucleus_id)
    if not peak or not nucleus or peak["isotope"] != nucleus["isotope"]:
        return False
    return bool(_shift_match(peak, nucleus, params)["within"])


def _correlation_score(state: dict[str, Any], graph: Graph, nucleus: dict[str, Any], peak: dict[str, Any], params: dict[str, Any]) -> tuple[float, list[dict[str, Any]], list[str]]:
    evidence: list[dict[str, Any]] = []
    expected_keys: list[str] = []
    score = 0.0
    nucleus_id = nucleus["id"]
    isotope = nucleus["isotope"]
    peak_id = peak["id"]

    if isotope == "1H":
        carbon_id = nucleus.get("attached_carbon")
        if carbon_id:
            expected_keys.append(f"expected:HSQC:{nucleus_id}:{carbon_id}")
            for edge in _observed_edges_for_peak(state, peak_id):
                if edge["edge_type"] != "HSQC":
                    continue
                other_peak_id = _other_end(edge, peak_id)
                if _peak_matches_nucleus(state, other_peak_id, carbon_id, params):
                    score += 18.0
                    evidence.append({"edge_id": edge["id"], "type": "HSQC", "supports": carbon_id, "weight": 18.0})
        for other in state["nuclei"].values():
            if other.get("isotope") != "1H" or not other.get("attached_carbon") or not nucleus.get("attached_carbon"):
                continue
            distance = graph.distance(nucleus.get("attached_carbon"), other.get("attached_carbon"))
            if distance == 1:
                expected_keys.append(f"expected:COSY:{min(nucleus_id, other[id])}:{max(nucleus_id, other[id])}")
            if distance in (2, 3):
                expected_keys.append(f"expected:HMBC:{nucleus_id}:{other[id]}")
        for edge in _observed_edges_for_peak(state, peak_id):
            other_peak_id = _other_end(edge, peak_id)
            if edge["edge_type"] == "COSY":
                for other in state["nuclei"].values():
                    if other["id"] == nucleus_id or other.get("isotope") != "1H":
                        continue
                    if nucleus.get("attached_carbon") and other.get("attached_carbon") and graph.distance(nucleus["attached_carbon"], other["attached_carbon"]) == 1 and _peak_matches_nucleus(state, other_peak_id, other["id"], params):
                        score += 6.0
                        evidence.append({"edge_id": edge["id"], "type": "COSY", "supports": other["id"], "weight": 6.0})
                        break
            elif edge["edge_type"] == "HMBC":
                for carbon in state["nuclei"].values():
                    if carbon.get("isotope") != "13C":
                        continue
                    attached = nucleus.get("attached_carbon")
                    if attached and graph.distance(attached, carbon["id"]) in (2, 3) and _peak_matches_nucleus(state, other_peak_id, carbon["id"], params):
                        score += 10.0
                        evidence.append({"edge_id": edge["id"], "type": "HMBC", "supports": carbon["id"], "weight": 10.0})
                        break
    elif isotope == "13C":
        attached_hydrogens = [item["id"] for item in state["nuclei"].values() if item.get("isotope") == "1H" and item.get("attached_carbon") == nucleus_id]
        for hydrogen_id in attached_hydrogens:
            expected_keys.append(f"expected:HSQC:{hydrogen_id}:{nucleus_id}")
        for edge in _observed_edges_for_peak(state, peak_id):
            other_peak_id = _other_end(edge, peak_id)
            if edge["edge_type"] == "HSQC":
                for hydrogen_id in attached_hydrogens:
                    if _peak_matches_nucleus(state, other_peak_id, hydrogen_id, params):
                        score += 18.0
                        evidence.append({"edge_id": edge["id"], "type": "HSQC", "supports": hydrogen_id, "weight": 18.0})
                        break
            elif edge["edge_type"] == "HMBC":
                other_peak = state["peaks"].get(other_peak_id)
                if other_peak and other_peak.get("isotope") == "1H":
                    for hydrogen in state["nuclei"].values():
                        attached = hydrogen.get("attached_carbon")
                        if hydrogen.get("isotope") == "1H" and attached and graph.distance(attached, nucleus_id) in (2, 3) and _peak_matches_nucleus(state, other_peak_id, hydrogen["id"], params):
                            score += 10.0
                            evidence.append({"edge_id": edge["id"], "type": "HMBC", "supports": hydrogen["id"], "weight": 10.0})
                            break

    return min(34.0, score), evidence, sorted(set(expected_keys))


def _relation_dependencies(state: dict[str, Any], graph: Graph, nucleus: dict[str, Any], peak: dict[str, Any], params: dict[str, Any]) -> list[str]:
    deps: list[str] = []
    peak_id = peak["id"]
    for edge in _observed_edges_for_peak(state, peak_id):
        deps.append(f"edge:{edge['id']}")
        deps.append(
            "observed-edge:"
            + ":".join(
                [
                    edge["edge_type"],
                    min(edge["from_peak"], edge["to_peak"]),
                    max(edge["from_peak"], edge["to_peak"]),
                ]
            )
        )

    def add_potential(edge_type: str, other_peak_id: str) -> None:
        left, right = sorted((peak_id, other_peak_id))
        deps.append(f"observed-edge:{edge_type}:{left}:{right}")

    if nucleus["isotope"] == "1H":
        carbon_id = nucleus.get("attached_carbon")
        if carbon_id:
            for other_peak in state["peaks"].values():
                if other_peak["isotope"] == "13C" and _peak_matches_nucleus(state, other_peak["id"], carbon_id, params):
                    add_potential("HSQC", other_peak["id"])
        attached = nucleus.get("attached_carbon")
        for other_nucleus in state["nuclei"].values():
            if other_nucleus["id"] == nucleus["id"] or other_nucleus.get("isotope") != "1H" or not attached:
                continue
            other_attached = other_nucleus.get("attached_carbon")
            if not other_attached:
                continue
            distance = graph.distance(attached, other_attached)
            edge_type = "COSY" if distance == 1 else "HMBC" if distance in (2, 3) else None
            if edge_type:
                for other_peak in state["peaks"].values():
                    if other_peak["isotope"] == "1H" and _peak_matches_nucleus(state, other_peak["id"], other_nucleus["id"], params):
                        add_potential(edge_type, other_peak["id"])
    elif nucleus["isotope"] == "13C":
        attached_hydrogens = [item for item in state["nuclei"].values() if item.get("isotope") == "1H" and item.get("attached_carbon") == nucleus["id"]]
        for hydrogen in attached_hydrogens:
            for other_peak in state["peaks"].values():
                if other_peak["isotope"] == "1H" and _peak_matches_nucleus(state, other_peak["id"], hydrogen["id"], params):
                    add_potential("HSQC", other_peak["id"])
        for hydrogen in state["nuclei"].values():
            if hydrogen.get("isotope") != "1H":
                continue
            hydrogen_attached = hydrogen.get("attached_carbon")
            if hydrogen_attached and graph.distance(hydrogen_attached, nucleus["id"]) in (2, 3):
                for other_peak in state["peaks"].values():
                    if other_peak["isotope"] == "1H" and _peak_matches_nucleus(state, other_peak["id"], hydrogen["id"], params):
                        add_potential("HMBC", other_peak["id"])
    return sorted(set(deps))


def source_fingerprint(state: dict[str, Any]) -> str:
    material = {
        "peaks": {key: value.get("raw_text") for key, value in sorted(state["peaks"].items())},
        "edges": {key: value.get("raw_text") for key, value in sorted(state["edges"].items())},
        "nuclei": {key: value.get("raw_text") for key, value in sorted(state["nuclei"].items())},
        "bonds": [bond.get("raw_text") for bond in sorted(state["bonds"], key=lambda item: (item["from"], item["to"]))],
        "conditions": {key: value.get("raw_text") for key, value in sorted(state["conditions"].items())},
        "source_versions": state["source_order"],
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:16]


def generate_candidates(state: dict[str, Any], run_id: str, params: dict[str, Any]) -> dict[str, Any]:
    graph = build_graph(state)
    candidates: list[dict[str, Any]] = []
    for nucleus in sorted(state["nuclei"].values(), key=lambda item: item["id"]):
        nucleus_candidates: list[dict[str, Any]] = []
        for peak in sorted(state["peaks"].values(), key=lambda item: (item["isotope"], item["id"])):
            if peak["isotope"] != nucleus["isotope"]:
                continue
            shift = _shift_match(peak, nucleus, params)
            multiplicity = _multiplicity_score(peak, nucleus)
            integral, integral_reason = _integration_score(peak, nucleus)
            correlation, evidence, expected_relation_keys = _correlation_score(state, graph, nucleus, peak, params)
            reasons = [reason for reason in [shift.get("reject_reason"), integral_reason] if reason]
            score = round(shift["score"] * 0.5 + multiplicity + integral + correlation, 6)
            dependencies = [
                f"record:{peak['source_id']}:{peak['source_version']}:{peak['id']}",
                f"record:{nucleus['source_id']}:{nucleus['source_version']}:{nucleus['id']}",
                f"algorithm:{ALGORITHM_VERSION}",
                f"parameters:{param_fingerprint(params)}",
            ]
            dependencies.extend(f"condition:{condition_id}" for condition_id in _isotope_condition_ids(state, nucleus["isotope"]))
            dependencies.extend(expected_relation_keys)
            dependencies.extend(_relation_dependencies(state, graph, nucleus, peak, params))
            nucleus_candidates.append(
                {
                    "id": stable_id("candidate", nucleus["id"], peak["id"] or "UNASSIGNED", ALGORITHM_VERSION, param_fingerprint(params)),
                    "nucleus_id": nucleus["id"],
                    "peak_id": peak["id"],
                    "isotope": nucleus["isotope"],
                    "status": "rejected" if reasons else "legal",
                    "score": score,
                    "score_components": {
                        "chemical_shift": shift,
                        "multiplicity": multiplicity,
                        "integration": integral,
                        "correlation_network": round(correlation, 6),
                    },
                    "rejection_reasons": reasons,
                    "unknown_reason": None,
                    "evidence_edges": evidence,
                    "expected_relation_keys": expected_relation_keys,
                    "dependencies": sorted(set(dependencies)),
                    "direct_sources": [
                        {"kind": "peak", "source_id": peak["source_id"], "source_version": peak["source_version"], "record_id": peak["id"]},
                        {"kind": "nucleus", "source_id": nucleus["source_id"], "source_version": nucleus["source_version"], "record_id": nucleus["id"]},
                    ],
                    "overlap_allowed": bool(peak.get("overlap_group")) or int(nucleus.get("group_size", 1)) > 1,
                    "run_id": run_id,
                }
            )
        unassigned_dependencies = [
            f"record:{nucleus['source_id']}:{nucleus['source_version']}:{nucleus['id']}",
            f"algorithm:{ALGORITHM_VERSION}",
            f"parameters:{param_fingerprint(params)}",
        ]
        unassigned_dependencies.extend(f"condition:{condition_id}" for condition_id in _isotope_condition_ids(state, nucleus["isotope"]))
        nucleus_candidates.append(
            {
                "id": stable_id("candidate", nucleus["id"], "UNASSIGNED", ALGORITHM_VERSION, param_fingerprint(params)),
                "nucleus_id": nucleus["id"],
                "peak_id": None,
                "isotope": nucleus["isotope"],
                "status": "unknown",
                "score": None,
                "score_components": {},
                "rejection_reasons": [],
                "unknown_reason": "NO_UNIQUE_OBSERVED_PEAK_OR_EXCHANGEABLE_RESONANCE_MISSING",
                "evidence_edges": [],
                "expected_relation_keys": [],
                "dependencies": sorted(set(unassigned_dependencies)),
                "direct_sources": [
                    {"kind": "nucleus", "source_id": nucleus["source_id"], "source_version": nucleus["source_version"], "record_id": nucleus["id"]}
                ],
                "overlap_allowed": False,
                "run_id": run_id,
            }
        )
        legal = [item for item in nucleus_candidates if item["status"] == "legal"]
        best_score = max((item["score"] for item in legal), default=None)
        ranked = sorted(legal, key=lambda item: (-item["score"], item["peak_id"] or ""))
        for rank, item in enumerate(ranked, start=1):
            item["rank"] = rank
            item["is_best"] = best_score is not None and abs(item["score"] - best_score) <= 0.08
            item["tie_group"] = stable_id("tie", nucleus["id"], run_id, f"{best_score:.3f}") if item["is_best"] else None
        for item in nucleus_candidates:
            item.setdefault("rank", None)
            item.setdefault("is_best", False)
            item.setdefault("tie_group", None)
        candidates.extend(nucleus_candidates)

    return {
        "id": run_id,
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_fingerprint": param_fingerprint(params),
        "source_fingerprint": source_fingerprint(state),
        "input_fingerprint": stable_id("run-input", source_fingerprint(state), ALGORITHM_VERSION, param_fingerprint(params)),
        "params": deepcopy(params),
        "candidates": candidates,
        "freshness": "current",
        "superseded_by": None,
    }


def _record_keys(parsed: dict[str, Any], record_type: str) -> list[str]:
    if record_type == "bond":
        return [f"record:{bond['source_id']}:{bond['source_version']}:bond:{bond['from']}-{bond['to']}" for bond in parsed["bonds"]]
    collection = parsed[record_type]
    if isinstance(collection, dict):
        return [f"record:{value['source_id']}:{value['source_version']}:{value['id']}" for value in collection.values()]
    return []


def _change_keys(old_state: dict[str, Any], parsed: dict[str, Any], source_id: str, parent_version: str | None) -> list[str]:
    keys = [f"source:{source_id}"]
    if parent_version:
        keys.extend(
            f"record:{source_id}:{parent_version}:{record_id}"
            for collection in ("peaks", "edges", "nuclei", "conditions")
            for record_id in old_state[collection]
            if old_state[collection][record_id].get("source_id") == source_id
        )
        keys.extend(
            f"record:{source_id}:{parent_version}:bond:{bond['from']}-{bond['to']}"
            for bond in old_state["bonds"]
            if bond.get("source_id") == source_id
        )
    for record_type in ("peaks", "edges", "nuclei", "conditions", "bond"):
        keys.extend(_record_keys(parsed, record_type))
    if any(key.startswith("condition:") for key in keys):
        pass
    condition_ids = list(parsed["conditions"])
    keys.extend(f"condition:{condition_id}" for condition_id in condition_ids)
    isotopes = {isotope for condition in parsed["conditions"].values() for isotope in condition.get("isotopes", [])}
    keys.extend(f"condition-isotope:{isotope}" for isotope in isotopes)
    for edge in parsed["edges"].values():
        left, right = sorted((edge["from_peak"], edge["to_peak"]))
        keys.append(f"observed-edge:{edge['edge_type']}:{left}:{right}")
    return sorted(set(keys))


def _apply_source_submitted(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    source_id = payload["source_id"]
    version = payload["version"]
    parsed = payload["parsed"]
    parent_version = payload.get("parent_version")
    change_keys = payload.get("change_keys") or _change_keys(state, parsed, source_id, parent_version)

    if parent_version:
        for collection in ("peaks", "edges", "nuclei", "conditions"):
            for key in list(state[collection].keys()):
                if state[collection][key].get("source_id") == source_id:
                    del state[collection][key]
        state["bonds"] = [bond for bond in state["bonds"] if bond.get("source_id") != source_id]

    for collection in ("peaks", "edges", "nuclei", "conditions"):
        for record_id, record in parsed[collection].items():
            state[collection][record_id] = deepcopy(record)
    state["bonds"].extend(deepcopy(parsed["bonds"]))
    state["sources"][source_id] = {
        "id": source_id,
        "version": version,
        "parent_version": parent_version,
        "filename": payload.get("filename"),
        "media_type": payload.get("media_type"),
        "raw_content": payload.get("raw_content"),
        "submitted_event_id": event["event_id"],
        "change_keys": change_keys,
        "diagnostics": deepcopy(parsed.get("diagnostics", [])),
    }
    if source_id not in state["source_order"]:
        state["source_order"].append(source_id)
    state["diagnostics"].extend(deepcopy(parsed.get("diagnostics", [])))

    change_set = set(change_keys)
    for run in state["runs"].values():
        if run.get("freshness") != "current":
            continue
        affected = set()
        for candidate in run["candidates"]:
            if change_set.intersection(candidate["dependencies"]):
                affected.add(candidate["id"])
        if affected:
            run["freshness"] = "stale"
            run["stale_reason"] = "DIRECT_SOURCE_DEPENDENCY_CHANGED"
            run["stale_change_keys"] = sorted(change_set)
            run["affected_candidate_ids"] = sorted(affected)


def _mark_all_runs_stale(state: dict[str, Any], reason: str, change_keys: list[str] | None = None, isotope: str | None = None) -> None:
    for run in state["runs"].values():
        if run.get("freshness") != "current":
            continue
        affected = set()
        for candidate in run["candidates"]:
            if isotope is not None and candidate.get("isotope") != isotope:
                continue
            if change_keys is None or set(change_keys).intersection(candidate["dependencies"]) or isotope is not None:
                affected.add(candidate["id"])
        if affected:
            run["freshness"] = "stale"
            run["stale_reason"] = reason
            run["stale_change_keys"] = change_keys or []
            run["affected_candidate_ids"] = sorted(affected)


def _apply_parameters_revised(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    state["parameters"][payload["parameter_set"]] = {
        "version": payload["version"],
        "value": deepcopy(payload["parameters"]),
        "fingerprint": param_fingerprint(payload["parameters"]),
        "event_id": event["event_id"],
        "parent_version": payload.get("parent_version"),
        "scope": payload.get("scope", "global"),
        "isotope": payload.get("isotope"),
    }
    _mark_all_runs_stale(state, "PARAMETER_DEPENDENCY_CHANGED", [f"parameters:{param_fingerprint(payload['parameters'])}"])


def _apply_conditions_revised(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    isotopes = payload.get("isotopes", [])
    keys = [f"condition:{payload['condition_id']}", *(f"condition-isotope:{isotope}" for isotope in isotopes)]
    _mark_all_runs_stale(state, "CONDITION_DEPENDENCY_CHANGED", keys)


def _candidate_copy(candidate: dict[str, Any], new_run_id: str, carry: bool) -> dict[str, Any]:
    copied = deepcopy(candidate)
    copied["run_id"] = new_run_id
    if carry:
        copied["decision_carried_from_run_id"] = candidate.get("run_id", new_run_id)
    return copied


def _carry_decisions(old_state: dict[str, Any], new_run: dict[str, Any], carried_candidate_ids: set[str]) -> None:
    active_decisions = [
        decision
        for decision in old_state["decisions"]
        if decision.get("state") == "active" and decision.get("candidate_id") in carried_candidate_ids
    ]
    for decision in active_decisions:
        copied = deepcopy(decision)
        copied["source_decision_id"] = decision["id"]
        copied["id"] = stable_id("decision", new_run["id"], decision["candidate_id"], decision["editor_id"], decision["action"])
        copied["run_id"] = new_run["id"]
        copied["carried"] = True
        old_state["decisions"].append(copied)


def _apply_candidates_generated(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    run = deepcopy(payload["run"])
    run["event_id"] = event["event_id"]
    new_fingerprint = run["input_fingerprint"]
    prior = [item for item in state["runs"].values() if item.get("input_fingerprint") == new_fingerprint]
    prior_ids = {candidate["id"] for old_run in prior for candidate in old_run["candidates"]}
    if prior:
        for old_run in prior:
            old_run["freshness"] = "superseded"
            old_run["superseded_by"] = run["id"]

    current_before = [item for item in state["runs"].values() if item.get("freshness") == "current" and item["id"] != run["id"]]
    if current_before and not prior:
        latest = sorted(current_before, key=lambda item: item["id"])[-1]
        latest_candidates = {candidate["id"]: candidate for candidate in latest["candidates"]}
        new_ids = {candidate["id"] for candidate in run["candidates"]}
        unchanged_ids = set(latest_candidates).intersection(new_ids)
        if unchanged_ids:
            for candidate in run["candidates"]:
                if candidate["id"] in unchanged_ids:
                    candidate["decision_carried_from_run_id"] = latest["id"]
            _carry_decisions(state, run, unchanged_ids)

    state["runs"][run["id"]] = run
    state["run_order"].append(run["id"])


def _candidate_lookup(state: dict[str, Any], run_id: str, candidate_id: str) -> dict[str, Any] | None:
    run = state["runs"].get(run_id)
    if not run:
        return None
    for candidate in run["candidates"]:
        if candidate["id"] == candidate_id:
            return candidate
    return None


def _symmetry_related_candidate_ids(state: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    nucleus = state["nuclei"].get(candidate["nucleus_id"])
    if not nucleus:
        return [candidate["id"]]
    group = nucleus.get("equivalence_group")
    run = state["runs"].get(candidate["run_id"])
    if not run or not group:
        return [candidate["id"]]
    related_nuclei = {
        item["id"]
        for item in state["nuclei"].values()
        if item.get("equivalence_group") == group and item.get("isotope") == nucleus.get("isotope")
    }
    return sorted(
        item["id"]
        for item in run["candidates"]
        if item["nucleus_id"] in related_nuclei and item["peak_id"] == candidate["peak_id"]
    )


def _active_decisions_on_branch(state: dict[str, Any], branch_id: str) -> list[dict[str, Any]]:
    return [item for item in state["decisions"] if item.get("state") == "active" and item.get("branch_id") == branch_id]



def _assignment_conflicts(state: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    if decision["action"] != "accept":
        return []
    candidate = _candidate_lookup(state, decision["run_id"], decision["candidate_id"])
    if not candidate or not candidate.get("peak_id"):
        return []
    conflicts: list[dict[str, Any]] = []
    for prior in _active_decisions_on_branch(state, decision["branch_id"]):
        if prior["action"] != "accept" or prior["id"] == decision["id"]:
            continue
        prior_candidate = _candidate_lookup(state, prior["run_id"], prior["candidate_id"])
        if not prior_candidate:
            continue
        same_nucleus = prior_candidate["nucleus_id"] == candidate["nucleus_id"]
        same_peak = prior_candidate.get("peak_id") == candidate.get("peak_id")
        if same_nucleus and not same_peak:
            conflicts.append({
                "kind": "ALTERNATIVE_BEST_CANDIDATE",
                "existing_decision_id": prior["id"],
                "reason": "two editors accepted different peaks for the same nucleus",
            })
        elif same_peak and not same_nucleus and not (candidate.get("overlap_allowed") and prior_candidate.get("overlap_allowed")):
            conflicts.append({
                "kind": "NON_OVERLAPPING_PEAK_FORCED_UNIQUE",
                "existing_decision_id": prior["id"],
                "reason": "a non-overlapping peak was forced to map uniquely",
            })
    return conflicts


def _apply_decision_made(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    candidate = _candidate_lookup(state, payload["run_id"], payload["candidate_id"])
    related = _symmetry_related_candidate_ids(state, candidate) if candidate else [payload["candidate_id"]]
    decision = {
        "id": payload.get("decision_id") or stable_id("decision", payload["run_id"], payload["candidate_id"], payload["editor_id"], payload["action"], event["event_id"]),
        "branch_id": event["branch_id"],
        "run_id": payload["run_id"],
        "candidate_id": payload["candidate_id"],
        "nucleus_id": candidate["nucleus_id"] if candidate else payload.get("nucleus_id"),
        "peak_id": candidate.get("peak_id") if candidate else payload.get("peak_id"),
        "editor_id": payload["editor_id"],
        "action": payload["action"],
        "note": payload.get("note", ""),
        "state": "active",
        "event_id": event["event_id"],
        "propagated_candidate_ids": related,
        "machine_judgement": {
            "overlap_is_evidence_not_identity": True,
            "coupling_network_is_evidence_only": True,
            "replayable": True,
        },
    }
    conflicts = _assignment_conflicts(state, decision)
    if conflicts:
        decision["state"] = "conflicted"
        conflict = {
            "id": stable_id("conflict", event["event_id"], decision["id"]),
            "branch_id": event["branch_id"],
            "state": "open",
            "decision_ids": sorted({decision["id"], *(item["existing_decision_id"] for item in conflicts)}),
            "reasons": conflicts,
            "created_event_id": event["event_id"],
            "resolution_event_id": None,
            "resolution": None,
        }
        for prior_id in conflict["decision_ids"]:
            for prior_decision in state["decisions"]:
                if prior_decision["id"] == prior_id and prior_decision["state"] == "active":
                    prior_decision["state"] = "conflicted"
        state["conflicts"].append(conflict)
    state["decisions"].append(decision)


def _apply_decision_resolved(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    conflict = next((item for item in state["conflicts"] if item["id"] == payload["conflict_id"]), None)
    if not conflict:
        return
    conflict["state"] = "resolved"
    conflict["resolution_event_id"] = event["event_id"]
    conflict["resolution"] = {
        "winner_decision_id": payload["winner_decision_id"],
        "loser_decision_id": payload["loser_decision_id"],
        "editor_id": payload["editor_id"],
        "note": payload.get("note", ""),
        "kept_adopted_and_rejected_results": True,
    }
    for decision in state["decisions"]:
        if decision["id"] == payload["winner_decision_id"]:
            decision["state"] = "active"
            decision["resolution"] = "winner"
        elif decision["id"] == payload["loser_decision_id"]:
            decision["state"] = "resolved_lost"
            decision["resolution"] = "loser"


def _apply_decision_retracted(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    for decision in state["decisions"]:
        if decision["id"] == payload["decision_id"]:
            decision["state"] = "retracted"
            decision["retraction_event_id"] = event["event_id"]


def _apply_branch_created(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    state["branches"][payload["branch_id"]] = {
        "branch_id": payload["branch_id"],
        "label": payload.get("label", payload["branch_id"]),
        "parent": payload.get("parent_branch_id", "main"),
        "fork_event_id": payload.get("fork_event_id"),
        "created_event_id": event["event_id"],
    }


EVENT_HANDLERS = {
    "PROJECT_CREATED": lambda state, event: None,
    "SOURCE_SUBMITTED": _apply_source_submitted,
    "PARAMETERS_REVISED": _apply_parameters_revised,
    "CONDITIONS_REVISED": _apply_conditions_revised,
    "CANDIDATES_GENERATED": _apply_candidates_generated,
    "DECISION_MADE": _apply_decision_made,
    "DECISION_RESOLVED": _apply_decision_resolved,
    "DECISION_RETRACTED": _apply_decision_retracted,
    "BRANCH_CREATED": _apply_branch_created,
}


def _branch_lineage(branches: dict[str, dict[str, Any]], branch_id: str) -> set[str]:
    lineage = {branch_id}
    current = branch_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        branch = branches.get(current, {})
        parent = branch.get("parent")
        if parent:
            lineage.add(parent)
            current = parent
        else:
            break
    return lineage


def replay_events(events: list[dict[str, Any]], branch_id: str = "main", up_to_event_id: str | None = None) -> dict[str, Any]:
    state = initial_state()
    accepted: list[dict[str, Any]] = []
    stop = False
    for event in events:
        if event["kind"] == "BRANCH_CREATED":
            _apply_branch_created(state, event)
    for event in events:
        if up_to_event_id is not None and event["event_id"] == up_to_event_id:
            stop = True
        in_ancestral_lineage = event["branch_id"] == "main" or event["branch_id"] in _branch_lineage(state["branches"], branch_id)
        if event["branch_id"] != branch_id and event["kind"] in {"DECISION_MADE", "DECISION_RESOLVED", "DECISION_RETRACTED"}:
            in_ancestral_lineage = event["branch_id"] in _branch_lineage(state["branches"], branch_id)
        if in_ancestral_lineage:
            handler = EVENT_HANDLERS.get(event["kind"])
            if handler:
                handler(state, event)
            state["events"].append(event)
            accepted.append(event)
        if stop:
            break
    state["viewpoint"] = {"branch_id": branch_id, "up_to_event_id": up_to_event_id}
    return state
