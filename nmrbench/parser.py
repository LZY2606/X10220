"""Raw-file parser for the NMR workbench.

Accepted small formats (UTF-8 text, lines starting with ``#`` are comments):

peaks files (``@nucleus 1H|13C``, columns, ``@units`` row optional)::

    @nucleus 1H
    @kind peaks
    @conditions run1-cond
    id, shift, intensity, integral, multiplicity, J
    H1, 4.89, 12.0, 1, q, 6.5

correlation files::

    @kind cosy|hsqc|hmbc
    id, from, to, strength

conditions / skeleton / params files use ``key=value`` or bespoke sections.

Hard format errors raise ``ParseError``. Individual values that cannot be
converted stay in their cell as :class:`ValueCell` with a diagnostic.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import Peak, CorrelationEdge, Atom, Conditions, Skeleton, ParsedDocument, content_hash
from .units import ValueCell


class ParseError(ValueError):
    def __init__(self, message: str, line_no: Optional[int] = None):
        super().__init__(message)
        self.line_no = line_no


KIND_BY_SUFFIX = {
    "peaks-1h": "peaks-1H",
    "peaks-13c": "peaks-13C",
    "1h": "peaks-1H",
    "13c": "peaks-13C",
    "cosy": "cosy",
    "hsqc": "hsqc",
    "hmbc": "hmbc",
    "noesy": "noesy",
    "cond": "conditions",
    "conditions": "conditions",
    "skeleton": "skeleton",
    "mol": "skeleton",
    "params": "params",
}

CORRELATION_KINDS = {"cosy", "hsqc", "hmbc", "noesy"}
PEAK_KINDS = {"peaks-1H", "peaks-13C"}


def split_csv(line: str) -> list[str]:
    # minimal, deterministic CSV split (no embedded-comma quoting needed by the
    # case format, but quotes are tolerated).
    out, cur, quote = [], "", None
    for ch in line.strip():
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ",":
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def _directives(lines: list[tuple[int, str]]):
    """Pull @directive lines and return (directives, remaining data lines)."""
    directives: dict[str, str] = {}
    data: list[tuple[int, str]] = []
    for lineno, raw in lines:
        body = raw.strip()
        if body.startswith("@"):
            parts = body[1:].split(None, 1)
            directives[parts[0].strip().lower()] = parts[1].strip() if len(parts) > 1 else ""
        else:
            data.append((lineno, raw))
    return directives, data


def parse_document(source_id: str, version: int, filename: str, content: str,
                   kind_hint: Optional[str] = None) -> ParsedDocument:
    if not isinstance(content, str) or content.strip() == "":
        raise ParseError("empty document", None)
    raw_hash = content_hash(content)
    lines = [(i + 1, l) for i, l in enumerate(content.splitlines())]
    directives, data = _directives([(n, l) for n, l in lines if not l.strip().startswith("#")])

    kind = (kind_hint or directives.get("kind") or _suffix_kind(filename)).strip()
    if kind in ("peaks",):
        nucleus = directives.get("nucleus", "").strip()
        kind = "peaks-1H" if nucleus == "1H" else "peaks-13C" if nucleus == "13C" else ""
        if not kind:
            raise ParseError("@nucleus 1H or 13C required for peaks document")
    if kind not in PEAK_KINDS | CORRELATION_KINDS | {"conditions", "skeleton", "params"}:
        raise ParseError(f"unknown document kind {kind!r}")

    doc = ParsedDocument(
        source_id=source_id, kind=kind, version=version, filename=filename,
        raw_content=content, raw_hash=raw_hash, conditions_ref=directives.get("conditions") or None,
    )
    if kind in PEAK_KINDS:
        _parse_peaks(doc, kind, directives, data)
    elif kind in CORRELATION_KINDS:
        _parse_edges(doc, kind, data)
    elif kind == "conditions":
        _parse_conditions(doc, directives, data)
    elif kind == "skeleton":
        _parse_skeleton(doc, data)
    else:
        _parse_params(doc, data)
    return doc


def _suffix_kind(filename: str) -> str:
    base = filename.rsplit(".", 1)[-1].lower()
    if base in KIND_BY_SUFFIX:
        return KIND_BY_SUFFIX[base]
    raise ParseError(f"cannot infer document kind from filename {filename!r}")


def _parse_peaks(doc: ParsedDocument, kind: str, directives: dict[str, str],
                 data: list[tuple[int, str]]) -> None:
    nucleus = "1H" if kind == "peaks-1H" else "13C"
    doc.nuclei = [nucleus]
    header: Optional[list[str]] = None
    unit_row: Optional[dict[str, str]] = None
    for lineno, raw in data:
        cells = split_csv(raw)
        if not cells or cells == [""]:
            continue
        low = [c.lower() for c in cells]
        if header is None and "shift" in low:
            header = low
            continue
        if low and low[0] == "@units" or (cells and cells[0].lower() == "units"):
            vals = [c for c in low[1:]]
            unit_row = _zip_units(header or _default_header(nucleus), vals)
            continue
        if header is None:
            header = _default_header(nucleus)
        row = _row_dict(header, cells)
        pid = row.get("id") or f"PK{lineno:03d}"
        qid = f"pk:{doc.source_id}@v{doc.version}:{pid}"
        unit_map = unit_row or _default_units(nucleus)

        shift = ValueCell.parse(row.get("shift", ""), "chemical_shift",
                                unit_map.get("shift", "ppm"), basis=directives.get("reference"))
        intensity = ValueCell.parse(row.get("intensity", ""), "intensity",
                                    unit_map.get("intensity", "rel"))
        integral_raw = row.get("integral")
        integral = (
            ValueCell.parse(integral_raw, "integral", unit_map.get("integral", "rel"))
            if integral_raw not in (None, "") else None
        )
        mult = (row.get("multiplicity") or None)
        jc: list[ValueCell] = []
        for jraw in (row.get("j", "") or "").split(";"):
            jraw = jraw.strip()
            if jraw:
                jc.append(ValueCell.parse(jraw, "splitting", unit_map.get("j", "Hz")))
        peak = Peak(
            id=qid, local_id=pid, source_id=doc.source_id, version=doc.version,
            line_no=lineno, nucleus=nucleus, shift=shift, intensity=intensity,
            integral=integral, multiplicity=mult, j_couplings=jc, notes=row.get("notes"),
        )
        for cell, name in ((shift, "shift"), (intensity, "intensity"), (integral, "integral")):
            if cell is not None and cell.diagnostic:
                doc.diagnostics.append({
                    "level": "error-cell", "line": lineno, "field": name,
                    "raw": cell.raw, "message": cell.diagnostic,
                })
        for j in jc:
            if j.diagnostic:
                doc.diagnostics.append({
                    "level": "error-cell", "line": lineno, "field": "j",
                    "raw": j.raw, "message": j.diagnostic,
                })
        doc.peaks.append(peak)
    if header is None and not doc.peaks:
        doc.diagnostics.append({"level": "warning", "message": "no peak rows"})


def _default_header(nucleus: str) -> list[str]:
    return ["id", "shift", "intensity", "integral", "multiplicity", "j", "notes"]


def _default_units(nucleus: str) -> dict[str, str]:
    return {"shift": "ppm", "intensity": "rel", "integral": "rel", "j": "Hz"}


def _zip_units(header: list[str], vals: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, val in zip(header[1:] if header and header[0] != "id" else header, vals):
        if val:
            out[name] = val
    return out


def _row_dict(header: list[str], cells: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, val in zip(header, cells):
        out[name] = val
    return out


def _parse_edges(doc: ParsedDocument, kind: str, data: list[tuple[int, str]]) -> None:
    header: Optional[list[str]] = None
    for lineno, raw in data:
        cells = split_csv(raw)
        if not cells or cells == [""]:
            continue
        low = [c.lower() for c in cells]
        if header is None and ("from" in low or "from_peak" in low):
            header = [c.replace("from_peak", "from") for c in low]
            continue
        if header is None:
            header = ["id", "from", "to", "strength"]
        row = _row_dict(header, cells)
        if not row.get("from") or not row.get("to"):
            raise ParseError(f"{kind} edge requires from and to", lineno)
        eid = row.get("id") or f"{kind.upper()}{lineno:03d}"
        strength_raw = row.get("strength")
        strength = ValueCell.parse(strength_raw, "intensity", "rel") if strength_raw else None
        edge = CorrelationEdge(
            id=f"{kind}:{doc.source_id}@v{doc.version}:{eid}", source_id=doc.source_id,
            version=doc.version, line_no=lineno, kind=kind, from_ref=row["from"],
            to_ref=row["to"], strength=strength,
        )
        if strength and strength.diagnostic:
            doc.diagnostics.append({
                "level": "error-cell", "line": lineno, "field": "strength",
                "raw": strength.raw, "message": strength.diagnostic,
            })
        doc.edges.append(edge)


def _parse_conditions(doc: ParsedDocument, directives: dict[str, str],
                      data: list[tuple[int, str]]) -> None:
    kv: dict[str, str] = {}
    for lineno, raw in data:
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        if "=" not in body:
            raise ParseError(f"conditions line must be key=value: {body!r}", lineno)
        key, val = body.split("=", 1)
        kv[key.strip().lower()] = val.strip()
    temp = None
    if kv.get("temperature"):
        unit = kv.get("temperature_unit", "K")
        temp = ValueCell.parse(kv["temperature"], "temperature", unit)
        if temp.diagnostic:
            doc.diagnostics.append({"level": "error-cell", "line": 0, "field": "temperature",
                                    "raw": temp.raw, "message": temp.diagnostic})
    freq = None
    if kv.get("spectrometer_frequency"):
        unit = kv.get("frequency_unit", "MHz")
        freq = ValueCell.parse(kv["spectrometer_frequency"], "frequency", unit)
        if freq.diagnostic:
            doc.diagnostics.append({"level": "error-cell", "line": 0,
                                    "field": "spectrometer_frequency", "raw": freq.raw,
                                    "message": freq.diagnostic})
    doc.conditions = Conditions(
        source_id=doc.source_id, version=doc.version, solvent=kv.get("solvent"),
        temperature=temp, reference=kv.get("reference"), spectrometer_frequency=freq,
        note=kv.get("note"),
    )


def _parse_skeleton(doc: ParsedDocument, data: list[tuple[int, str]]) -> None:
    skel = Skeleton()
    section = "atoms"
    for lineno, raw in data:
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        if body.lower().startswith("@atoms"):
            section = "atoms"
            continue
        if body.lower().startswith("@bonds"):
            section = "bonds"
            continue
        if section == "atoms":
            parts = split_csv(body)
            if len(parts) < 2:
                raise ParseError(f"atom row needs id,element,...: {body!r}", lineno)
            aid, element = parts[0].strip(), parts[1].strip()
            if element not in ("H", "C"):
                raise ParseError(f"unsupported element {element!r} (only H/C nuclei)", lineno)
            rest = {k.strip(): v.strip() for k, v in
                    (p.split("=", 1) for p in parts[2:] if "=" in p)}
            pred = None
            if rest.get("predicted_shift"):
                pred = ValueCell.parse(rest["predicted_shift"], "chemical_shift", "ppm")
                if pred.diagnostic:
                    doc.diagnostics.append({"level": "error-cell", "line": lineno,
                                            "field": "predicted_shift", "raw": pred.raw,
                                            "message": pred.diagnostic})
            skel.atoms.append(Atom(
                id=aid, element=element, attached_to=rest.get("attached_to") or None,
                equivalent_group=rest.get("equivalent_group") or None,
                multiplicity=rest.get("multiplicity", "u"),
                predicted_shift=pred, label=rest.get("label") or None,
            ))
        else:
            parts = split_csv(body)
            if len(parts) < 2:
                raise ParseError(f"bond row needs id,id: {body!r}", lineno)
            skel.bonds.append((parts[0].strip(), parts[1].strip()))
    if not skel.atoms:
        raise ParseError("skeleton has no atoms")
    doc.skeleton = skel


def _parse_params(doc: ParsedDocument, data: list[tuple[int, str]]) -> None:
    # Params documents are parsed into the raw form; numeric conversion happens
    # in the service layer where validation errors become hard diagnostics.
    doc.diagnostics.append({"level": "note", "message": "params carried as raw key=value"})
