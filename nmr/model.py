from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Atom:
    atom_id: str
    label: str
    element: str
    nucleus: str
    shift: float | None
    shift_unit: str
    multiplicity: str | None
    group_integral: float | None
    equivalent_group: str | None
    x: float
    y: float
    source_doc_id: str


@dataclass(frozen=True)
class Peak:
    peak_id: str
    nucleus: str
    shift: float
    shift_unit: str
    intensity: float | None
    integral: float | None
    multiplicity: str | None
    source_doc_id: str
    source_line: int
    raw_text: str


@dataclass(frozen=True)
class Edge:
    edge_id: str
    experiment: str
    from_shift: float
    from_unit: str
    to_shift: float
    to_unit: str
    intensity: float | None
    source_doc_id: str
    source_line: int
    raw_text: str


@dataclass(frozen=True)
class ExpectedEdge:
    from_atom: str
    to_atom: str
    kind: str


def build_model(active_sources: list[dict[str, Any]]) -> dict[str, Any]:
    molecule: dict[str, Any] | None = None
    peaks: list[Peak] = []
    edges: list[Edge] = []
    diagnostics: list[dict[str, Any]] = []
    for source in active_sources:
        parsed = source["parsed"]
        diagnostics.extend({"source_doc_id": source["doc_id"], **item} for item in parsed.get("diagnostics", []))
        if parsed["kind"] == "molecule":
            molecule = _build_molecule(source, parsed, diagnostics)
        elif parsed["kind"] == "peak1d":
            meta = parsed["records"]["meta"][0]
            for record in parsed["records"]["peaks"]:
                peaks.append(Peak(
                    peak_id=f"{source['doc_id']}:P:{record['id']}",
                    nucleus=str(record.get("nucleus", meta.get("nucleus", ""))),
                    shift=float(record["shift"]),
                    shift_unit=str(record.get("shift_unit", "ppm")),
                    intensity=_maybe_float(record.get("intensity")),
                    integral=_maybe_float(record.get("integral")),
                    multiplicity=record.get("multiplicity"),
                    source_doc_id=source["doc_id"],
                    source_line=int(record["line"]),
                    raw_text=str(record["raw_text"]),
                ))
        elif parsed["kind"] == "peak2d":
            for record in parsed["records"]["edges"]:
                edges.append(Edge(
                    edge_id=f"{source['doc_id']}:E:{record['id']}",
                    experiment=str(record["experiment"]).upper(),
                    from_shift=float(record["from_shift"]),
                    from_unit=str(record.get("from_shift_unit", "ppm")),
                    to_shift=float(record["to_shift"]),
                    to_unit=str(record.get("to_shift_unit", "ppm")),
                    intensity=_maybe_float(record.get("intensity")),
                    source_doc_id=source["doc_id"],
                    source_line=int(record["line"]),
                    raw_text=str(record["raw_text"]),
                ))
    return {"molecule": molecule, "peaks": peaks, "edges": edges, "diagnostics": diagnostics}


def _build_molecule(source: dict[str, Any], parsed: dict[str, Any], diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    meta = parsed["records"]["meta"][0]
    atoms: dict[str, Atom] = {}
    bonds: list[tuple[str, str]] = []
    expected: list[ExpectedEdge] = []
    for record in parsed["records"]["atoms"]:
        atom_id = str(record["id"])
        atoms[atom_id] = Atom(
            atom_id=atom_id,
            label=str(record.get("label", atom_id)),
            element=str(record["element"]),
            nucleus=str(record.get("nucleus", "1H" if record.get("element") == "H" else "13C")),
            shift=_maybe_float(record.get("shift")),
            shift_unit=str(record.get("shift_unit", "ppm")),
            multiplicity=record.get("multiplicity"),
            group_integral=_maybe_float(record.get("group_integral")),
            equivalent_group=record.get("eq_group"),
            x=float(record.get("x", 0.0)),
            y=float(record.get("y", 0.0)),
            source_doc_id=source["doc_id"],
        )
    for record in parsed["records"]["bonds"]:
        left, right = str(record["from"]), str(record["to"])
        if left in atoms and right in atoms:
            bonds.append((left, right))
            if str(record.get("couple", "false")).lower() == "true":
                expected.append(ExpectedEdge(left, right, "COSY"))
        else:
            diagnostics.append({"source_doc_id": source["doc_id"], "line": record["line"], "code": "dangling_bond", "message": "bond retained as diagnostic", "raw_text": record["raw_text"]})
    return {
        "source_doc_id": source["doc_id"],
        "name": meta.get("name", "Unnamed molecule"),
        "meta": meta,
        "atoms": atoms,
        "bonds": bonds,
        "expected_edges": expected,
    }


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
