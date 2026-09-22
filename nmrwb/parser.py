"""Import layer for the offline NMR case-pack text format.

Four source kinds share one small, copy-pasteable block format::

    @nbr-case/1h
    # key: value meta lines (solvent, frequency, reference, temperature ...)
    PEAK id shift ppm, intensity a.u., integral count, multiplicity
    ...

Nothing here is overwritten: the caller submits the whole text as an immutable
version.  Original tokens are kept on every quantity; unconvertible values are
kept and reported as diagnostics instead of being dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .units import parse_quantity, Quantity

VALID_KINDS = {"1h", "13c", "cosy", "hsqc", "hmbc", "skeleton", "rules"}


@dataclass
class Diagnostic:
    code: str
    message: str
    line: int
    ref: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "line": self.line, "ref": self.ref}


@dataclass
class Peak:
    pid: str
    shift: Quantity
    intensity: Quantity
    integral: Quantity
    multiplicity: str
    source_id: str
    version_id: str
    line: int

    def to_dict(self) -> dict:
        return {
            "id": f"{self.source_id}/{self.version_id}/P:{self.pid}",
            "pid": self.pid,
            "shift": self.shift.to_dict(),
            "intensity": self.intensity.to_dict(),
            "integral": self.integral.to_dict(),
            "multiplicity": self.multiplicity,
            "source_id": self.source_id,
            "version_id": self.version_id,
            "line": self.line,
        }


@dataclass
class Edge:
    eid: str
    dim1: str
    dim2: str
    intensity: Quantity
    kind: str
    source_id: str
    version_id: str
    line: int

    def to_dict(self) -> dict:
        return {
            "id": f"{self.source_id}/{self.version_id}/E:{self.eid}",
            "eid": self.eid,
            "dim1": self.dim1,
            "dim2": self.dim2,
            "intensity": self.intensity.to_dict(),
            "kind": self.kind,
            "source_id": self.source_id,
            "version_id": self.version_id,
            "line": self.line,
        }


@dataclass
class Atom:
    label: str
    element: str
    isotope: str
    symmetry_class: str
    equiv_size: int
    coord: tuple[float, float]
    line: int

    def to_dict(self) -> dict:
        return {"label": self.label, "element": self.element, "isotope": self.isotope,
                "symmetry_class": self.symmetry_class, "equiv_size": self.equiv_size,
                "coord": list(self.coord), "line": self.line}


@dataclass
class Bond:
    a: str
    b: str
    order: int
    line: int

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "order": self.order, "line": self.line}


@dataclass
class Prior:
    label: str
    isotope: str
    expected_ppm: Quantity
    tolerance_ppm: Quantity
    line: int

    def to_dict(self) -> dict:
        return {"label": self.label, "isotope": self.isotope,
                "expected_ppm": self.expected_ppm.to_dict(),
                "tolerance_ppm": self.tolerance_ppm.to_dict(), "line": self.line}


@dataclass
class ParsedSource:
    kind: str
    meta: dict
    peaks: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    atoms: list = field(default_factory=list)
    bonds: list = field(default_factory=list)
    priors: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "meta": self.meta,
            "peaks": [p.to_dict() for p in self.peaks],
            "edges": [e.to_dict() for e in self.edges],
            "atoms": [a.to_dict() for a in self.atoms],
            "bonds": [b.to_dict() for b in self.bonds],
            "priors": [p.to_dict() for p in self.priors],
            "params": self.params,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _split_csv(line: str) -> list:
    return [c.strip() for c in line.split(",") if c.strip() != ""]


def _take_q(token: str, default_unit: str, basis: str,
            diags: list, line: int, ref: str) -> Quantity:
    q = parse_quantity(token, default_unit, basis)
    if not q.ok:
        diags.append(Diagnostic("UNCONVERTIBLE_VALUE",
                                q.message or "cannot convert", line, ref))
    return q


def _header(kind_line: str, diags: list, line: int) -> str:
    parts = kind_line.strip().split("/")
    if len(parts) != 2 or parts[0] != "@nbr-case":
        raise FormatError(f"first line must be '@nbr-case/<kind>', got {kind_line!r}", line)
    kind = parts[1].strip().lower()
    if kind not in VALID_KINDS:
        raise FormatError(f"unknown source kind {kind!r}; expected one of "
                          f"{sorted(VALID_KINDS)}", line)
    return kind


class FormatError(ValueError):
    def __init__(self, message: str, line: int = 0):
        super().__init__(f"line {line}: {message}")
        self.line = line


def parse_source(text: str, source_id: str, version_id: str) -> ParsedSource:
    lines = text.splitlines()
    if not any(l.strip() for l in lines):
        raise FormatError("empty source", 0)
    diags: list = []
    kind = None
    start = 0
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if s:
            kind = _header(s, diags, i)
            start = i
            break
    if kind is None:
        raise FormatError("missing '@nbr-case/<kind>' header", 1)

    parsed = ParsedSource(kind=kind, meta={}, diagnostics=diags)
    seen_ids: set = set()

    for idx in range(start, len(lines)):
        lineno = idx + 1
        s = lines[idx].strip()
        if not s:
            continue
        # Comment lines of the form ``# key: value`` carry provenance meta
        # (solvent, temperature, frequency, reference ...).  Pure comments
        # without a colon are ignored.
        if s.startswith("#"):
            body = s.lstrip("#").strip()
            if ":" in body:
                key, _, value = body.partition(":")
                parsed.meta[key.strip()] = value.strip()
            continue
        upper = s.upper()
        ref = f"{source_id}/{version_id}"

        if upper.startswith("PEAK "):
            if kind not in ("1h", "13c"):
                raise FormatError(f"PEAK rows are invalid in @nbr-case/{kind}", lineno)
            cells = _split_csv(s[5:])
            if len(cells) < 4:
                raise FormatError("PEAK requires id, shift, intensity, integral[, multiplicity]",
                                  lineno)
            pid = cells[0]
            if pid in seen_ids:
                raise FormatError(f"duplicate peak id {pid!r}", lineno)
            seen_ids.add(pid)
            shift = _take_q(cells[1], "ppm", "chemical-shift", diags, lineno, ref)
            intensity = _take_q(cells[2], "a.u.", "intensity", diags, lineno, ref)
            integral = _take_q(cells[3], "count", "integral", diags, lineno, ref)
            mult = cells[4] if len(cells) > 4 else "u"
            parsed.peaks.append(Peak(pid, shift, intensity, integral, mult,
                                     source_id, version_id, lineno))
        elif upper.startswith("EDGE "):
            if kind not in ("cosy", "hsqc", "hmbc"):
                raise FormatError(f"EDGE rows are invalid in @nbr-case/{kind}", lineno)
            cells = _split_csv(s[5:])
            if len(cells) < 3:
                raise FormatError("EDGE requires id, dim1, dim2[, intensity]", lineno)
            eid = cells[0]
            if eid in seen_ids:
                raise FormatError(f"duplicate edge id {eid!r}", lineno)
            seen_ids.add(eid)
            intensity = _take_q(cells[3] if len(cells) > 3 else "1",
                                "a.u.", "correlation-intensity", diags, lineno, ref)
            parsed.edges.append(Edge(eid, cells[1], cells[2], intensity,
                                     kind, source_id, version_id, lineno))
        elif upper.startswith("ATOM "):
            if kind != "skeleton":
                raise FormatError("ATOM rows are invalid outside @nbr-case/skeleton", lineno)
            cells = _split_csv(s[5:])
            # ATOM label, element, isotope, symmetryClass, equivSize, x, y
            if len(cells) != 7:
                raise FormatError("ATOM requires label, element, isotope, class, size, x, y",
                                  lineno)
            try:
                size = int(cells[4]); x = float(cells[5]); y = float(cells[6])
            except ValueError:
                raise FormatError("bad numeric ATOM coordinate/size", lineno)
            label = cells[0]
            if label in seen_ids:
                raise FormatError(f"duplicate atom label {label!r}", lineno)
            seen_ids.add(label)
            parsed.atoms.append(Atom(label, cells[1], cells[2], cells[3],
                                     size, (x, y), lineno))
        elif upper.startswith("BOND "):
            if kind != "skeleton":
                raise FormatError("BOND rows are invalid outside @nbr-case/skeleton", lineno)
            cells = _split_csv(s[5:])
            if len(cells) != 3:
                raise FormatError("BOND requires a, b, order", lineno)
            try:
                order = int(cells[2])
            except ValueError:
                raise FormatError("bond order must be an integer", lineno)
            parsed.bonds.append(Bond(cells[0], cells[1], order, lineno))
        elif upper.startswith("PRIOR "):
            if kind != "rules":
                raise FormatError("PRIOR rows are invalid outside @nbr-case/rules", lineno)
            cells = _split_csv(s[6:])
            if len(cells) != 4:
                raise FormatError("PRIOR requires label, isotope, expected_ppm, tolerance_ppm",
                                  lineno)
            expected = _take_q(cells[2], "ppm", "prior-expected", diags, lineno, ref)
            tol = _take_q(cells[3], "ppm", "prior-tolerance", diags, lineno, ref)
            parsed.priors.append(Prior(cells[0], cells[1], expected, tol, lineno))
        elif upper.startswith("PARAM "):
            if kind != "rules":
                raise FormatError("PARAM rows are invalid outside @nbr-case/rules", lineno)
            cells = _split_csv(s[6:])
            if len(cells) != 2:
                raise FormatError("PARAM requires name, value", lineno)
            name = cells[0]
            raw_v = cells[1]
            try:
                if "." in raw_v:
                    parsed.params[name] = float(raw_v)
                else:
                    parsed.params[name] = int(raw_v)
            except ValueError:
                parsed.params[name] = raw_v
        elif ":" in s and not s.startswith("@"):
            key, _, value = s.partition(":")
            key = key.strip().lstrip("# ").strip()
            parsed.meta[key.strip()] = value.strip()
        else:
            raise FormatError(f"unrecognized record {s[:40]!r}", lineno)

    _validate_cross_refs(parsed, diags)
    return parsed


def _validate_cross_refs(parsed: ParsedSource, diags: list) -> None:
    if parsed.kind == "skeleton":
        labels = {a.label for a in parsed.atoms}
        for b in parsed.bonds:
            if b.a not in labels or b.b not in labels:
                diags.append(Diagnostic("DANGLING_BOND",
                                        f"bond {b.a}-{b.b} references unknown atom",
                                        b.line, "skeleton"))
    if parsed.kind == "rules":
        # priors can't be cross-checked here (skeleton lives elsewhere);
        # the engine performs that check.
        pass
