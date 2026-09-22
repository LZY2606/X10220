"""NMR peak-list / correlation / skeleton importer.

Accepts a small, copy-pastable line-oriented text format (``.nmr``):

    @document h1_v1
    @kind 1d
    @nucleus 1H
    @solvent CDCl3
    @temperature 298 K
    @reference TMS 0.0 ppm
    @spectrometer_frequency 400.13 MHz
    @columns id shift intensity integral multiplicity j_hz
    peak Hp1 1.234 ppm 1000 a.u. 3.0 H-equiv d 7.1 Hz
    peak Hp2 1.240 ppm  980 a.u. 3.0 H-equiv d 7.0 Hz
    peak Hp3 2.450 ppm 1500 a.u. 1.0 H-equiv m

2D documents use columns ``id shift_f1 shift_f2 intensity multiplicity`` and an
``@experiment COSY|HSQC|HMBC`` header; a ``skeleton`` kind documents atoms/bonds.

Rules:
  * The raw bytes/text are retained verbatim, together with filename/version.
  * Parse errors are *explicit*: a malformed line is a fatal diagnostic (the
    document is rejected) so format errors cannot be silently swallowed.
  * Unconvertible measurements are non-fatal: the line stays, the value keeps
    its raw spelling and carries a diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from .units import parse_quantity, Diagnostic, Quantity
from .fingerprint import sha256_text

VALID_KINDS = {"1d", "2d", "skeleton"}
VALID_EXPERIMENTS = {"COSY", "HSQC", "HMBC", "TOCSY", "NOESY"}
MULTIPLICITIES = {"s", "d", "t", "q", "m", "dd", "dt", "td", "br", "brs"}


@dataclass
class Peak:
    id: str
    shift: Quantity
    intensity: Optional[Quantity] = None
    integral: Optional[Quantity] = None
    multiplicity: Optional[str] = None
    j_couplings: List[Quantity] = field(default_factory=list)
    raw_line: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "shift": self.shift.to_json(),
            "intensity": self.intensity.to_json() if self.intensity else None,
            "integral": self.integral.to_json() if self.integral else None,
            "multiplicity": self.multiplicity,
            "j_couplings": [j.to_json() for j in self.j_couplings],
            "raw_line": self.raw_line,
        }


@dataclass
class CorrelationEdge:
    id: str
    experiment: str
    shift_f1: Quantity
    shift_f2: Quantity
    intensity: Optional[Quantity] = None
    multiplicity: Optional[str] = None
    raw_line: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "experiment": self.experiment,
            "shift_f1": self.shift_f1.to_json(),
            "shift_f2": self.shift_f2.to_json(),
            "intensity": self.intensity.to_json() if self.intensity else None,
            "multiplicity": self.multiplicity,
            "raw_line": self.raw_line,
        }


@dataclass
class SkeletonAtom:
    id: str
    element: str
    nucleus: str            # e.g. 1H, 13C
    predicted_shift: Optional[Quantity] = None
    x: Optional[float] = None
    y: Optional[float] = None
    symmetry_group: Optional[str] = None
    exchangeable: bool = False
    raw_line: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "element": self.element,
            "nucleus": self.nucleus,
            "predicted_shift": self.predicted_shift.to_json()
            if self.predicted_shift else None,
            "x": self.x, "y": self.y,
            "symmetry_group": self.symmetry_group,
            "exchangeable": self.exchangeable,
            "raw_line": self.raw_line,
        }


@dataclass
class SkeletonBond:
    id: str
    a: str
    b: str
    order: int = 1
    raw_line: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "a": self.a, "b": self.b, "order": self.order,
                "raw_line": self.raw_line}


@dataclass
class SourceDocument:
    """Immutable submitted source input (peak list / skeleton)."""

    doc_id: str
    kind: str
    filename: str
    version: str
    parent_version: Optional[str]
    raw_text: str
    content_hash: str
    media_type: str
    meta: Dict[str, Any] = field(default_factory=dict)
    peaks: List[Peak] = field(default_factory=list)
    edges: List[CorrelationEdge] = field(default_factory=list)
    atoms: List[SkeletonAtom] = field(default_factory=list)
    bonds: List[SkeletonBond] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def to_json(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "kind": self.kind,
            "filename": self.filename,
            "version": self.version,
            "parent_version": self.parent_version,
            "raw_text": self.raw_text,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "meta": self.meta,
            "peaks": [p.to_json() for p in self.peaks],
            "edges": [e.to_json() for e in self.edges],
            "atoms": [a.to_json() for a in self.atoms],
            "bonds": [b.to_json() for b in self.bonds],
            "diagnostics": [d.to_json() for d in self.diagnostics],
            "accepted": self.accepted,
        }


# --- line helpers ---------------------------------------------------------

def _tokens_for(line, n_min):
    toks = line.split()
    if len(toks) < n_min:
        return None, f"expected at least {n_min} tokens, got {len(toks)}"
    return toks, None


def _take_q(tokens, start, kind, basis=None):
    """Consume '<number> <unit>' starting at index ``start``; return (q, next)."""
    if start >= len(tokens):
        return parse_quantity(None, kind, basis), start
    num = tokens[start]
    unit = tokens[start + 1] if start + 1 < len(tokens) else None
    # unit token heuristic: starts with a letter or degree sign
    if unit and (unit[0].isalpha() or unit[0] in "°%"):
        q = parse_quantity(num + " " + unit, kind, basis)
        return q, start + 2
    q = parse_quantity(num, kind, basis)
    return q, start + 1


def _parse_peak_line(line, columns, ref_basis):
    # columns sequence drives parsing so "intensity" and "integral" (both
    # numeric) cannot be confused. Multiplicity must be a known token.
    toks = line.split()
    pid = toks[1]
    idx = 2
    shift = intensity = integral = None
    mult = None
    js: List[Quantity] = []
    diags: List[Diagnostic] = []
    if "shift" in columns:
        shift, idx = _take_q(toks, idx, "chemical_shift", ref_basis)
        diags.extend(shift.diagnostics)
    if "intensity" in columns:
        intensity, idx = _take_q(toks, idx, "intensity")
        diags.extend(intensity.diagnostics)
    if "integral" in columns:
        integral, idx = _take_q(toks, idx, "integral")
        diags.extend(integral.diagnostics)
    if "multiplicity" in columns and idx < len(toks):
        cand = toks[idx]
        if cand not in MULTIPLICITIES:
            diags.append(Diagnostic("BAD_MULTIPLICITY",
                                    f"multiplicity {cand!r} not recognised", line))
        else:
            mult = cand
            idx += 1
    while idx < len(toks):
        q, idx = _take_q(toks, idx, "coupling")
        js.append(q)
        diags.extend(q.diagnostics)
    p = Peak(id=pid, shift=shift, intensity=intensity, integral=integral,
             multiplicity=mult, j_couplings=js, raw_line=line)
    p._diags = diags  # type: ignore[attr-defined]
    return p


def _parse_edge_line(line, experiment, ref_basis):
    toks = line.split()
    idx = 2
    eid = toks[1]
    f1, idx = _take_q(toks, idx, "chemical_shift", ref_basis)
    f2, idx = _take_q(toks, idx, "chemical_shift", ref_basis)
    intensity = None
    mult = None
    if idx < len(toks) and (toks[idx][0].isdigit() or toks[idx][0] in "+-."):
        intensity, idx = _take_q(toks, idx, "intensity")
    if idx < len(toks) and toks[idx] in MULTIPLICITIES:
        mult = toks[idx]
        idx += 1
    return CorrelationEdge(id=eid, experiment=experiment, shift_f1=f1, shift_f2=f2,
                           intensity=intensity, multiplicity=mult, raw_line=line)


def _parse_atom_line(line):
    toks = line.split()
    # atom <id> <element> <nucleus> predicted <num ppm> at <x> <y>
    #      [symmetry <g>] [exchangeable]
    idx = 2
    aid, element, nucleus = toks[1], toks[2], toks[3]
    idx = 4
    pred = None
    x = y = None
    sym = None
    exch = False
    while idx < len(toks):
        t = t = toks[idx]
        if t == "predicted":
            pred, idx = _take_q(toks, idx + 1, "chemical_shift", "predicted")
        elif t == "at":
            x = float(toks[idx + 1]); y = float(toks[idx + 2]); idx += 3
        elif t == "symmetry":
            sym = toks[idx + 1]; idx += 2
        elif t == "exchangeable":
            exch = True; idx += 1
        else:
            idx += 1
    return SkeletonAtom(id=aid, element=element, nucleus=nucleus,
                        predicted_shift=pred, x=x, y=y, symmetry_group=sym,
                        exchangeable=exch, raw_line=line)


def parse_document(raw_text, filename, doc_id=None, version=None,
                   parent_version=None, media_type="text/vnd.nmr-workbench"):
    """Parse raw text into a :class:`SourceDocument`.

    Fatal diagnostics mark the document rejected; raw text is always retained.
    """
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    meta = {"nucleus": None, "experiment": None}
    diags: List[Diagnostic] = []
    peaks, edges, atoms, bonds = [], [], [], []
    kind = None
    declared_id = None
    declared_version = "1"
    parent = None
    ref = None

    def err(code, msg, raw=None):
        diags.append(Diagnostic(code, msg, raw, "error"))

    for lineno, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            parts = line.split(None, 1)
            key = parts[0].lower()
            val = parts[1].strip() if len(parts) > 1 else ""
            if key == "@document":
                declared_id = val
            elif key == "@version":
                declared_version = val
            elif key == "@parent_version":
                parent = val
            elif key == "@kind":
                if val not in VALID_KINDS:
                    err("UNKNOWN_KIND", f"kind {val!r} not in {sorted(VALID_KINDS)}",
                        line)
                kind = val
            elif key == "@nucleus":
                meta["nucleus"] = val
            elif key == "@experiment":
                up = val.upper()
                if up not in VALID_EXPERIMENTS:
                    err("UNKNOWN_EXPERIMENT", f"experiment {val!r} not supported", line)
                meta["experiment"] = up
            elif key == "@solvent":
                meta["solvent"] = val
            elif key == "@temperature":
                q = parse_quantity(val, "temperature")
                meta["temperature_raw"] = val
                meta["temperature_k"] = q.canonical_value
                diags.extend(q.diagnostics)
            elif key == "@reference":
                ref = val
                meta["reference"] = val
            elif key == "@spectrometer_frequency":
                q = parse_quantity(val, "frequency")
                meta["spectrometer_frequency_raw"] = val
                meta["spectrometer_frequency_mhz"] = q.canonical_value
                diags.extend(q.diagnostics)
            elif key == "@columns":
                meta["columns"] = val.split()
            else:
                err("UNKNOWN_HEADER", f"unknown header {key!r}", line)
            continue

        head = line.split(None, 1)[0]
        try:
            if head == "peak":
                if kind != "1d":
                    err("PEAK_IN_NON_1D", "peak row outside a @kind 1d document", line)
                    continue
                cols = meta.get("columns") or [
                    "id", "shift", "intensity", "integral", "multiplicity"]
                p = _parse_peak_line(line, cols, ref)
                diags.extend(getattr(p, "_diags", []))
                peaks.append(p)
            elif head == "edge":
                if kind != "2d":
                    err("EDGE_IN_NON_2D", "edge row outside a @kind 2d document", line)
                    continue
                e = _parse_edge_line(line, meta.get("experiment"), ref)
                for q in [e.shift_f1, e.shift_f2,
                          *( [e.intensity] if e.intensity else [])]:
                    diags.extend(q.diagnostics)
                edges.append(e)
            elif head == "atom":
                if kind != "skeleton":
                    err("ATOM_IN_NON_SKELETON", "atom outside a skeleton document", line)
                    continue
                try:
                    atoms.append(_parse_atom_line(line))
                except (IndexError, ValueError) as exc:
                    err("BAD_ATOM_ROW", f"malformed atom row: {exc}", line)
            elif head == "bond":
                toks = line.split()
                if len(toks) < 4:
                    err("BAD_BOND_ROW", "bond <id> <a> <b> [order]", line)
                else:
                    order = int(toks[4]) if len(toks) > 4 else 1
                    bonds.append(SkeletonBond(toks[1], toks[2], toks[3], order, line))
            else:
                err("UNKNOWN_ROW", f"cannot parse line starting {head!r}", line)
        except IndexError:
            err("MALFORMED_ROW", f"line {lineno}: missing fields", line)

    if kind is None:
        err("MISSING_KIND", "document has no @kind declaration")
    if kind == "1d" and meta.get("nucleus") is None:
        err("MISSING_NUCLEUS", "1D document requires @nucleus")
    if kind == "2d" and meta.get("experiment") is None:
        err("MISSING_EXPERIMENT", "2D document requires @experiment")

    doc = SourceDocument(
        doc_id=doc_id or declared_id or filename,
        kind=kind or "unknown",
        filename=filename,
        version=version or declared_version,
        parent_version=parent_version if parent_version is not None else None,
        raw_text=text,
        content_hash=sha256_text(text),
        media_type=media_type,
        meta=meta,
        peaks=peaks, edges=edges, atoms=atoms, bonds=bonds,
        diagnostics=diags,
    )
    return doc
