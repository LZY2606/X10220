"""Import layer.

Accepts tiny copy-pasteable text samples and reports explicit format errors.
Every numeric token keeps its raw representation; only derived layers
normalise.  A value that cannot be converted is kept in place with a
diagnostic instead of being dropped.

Hard format errors (missing header, wrong column count, unknown nucleus/kind)
raise :class:`IngestError` and abort that document.  Soft problems are
collected as diagnostics while the offending entity is still stored.
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import (Edge, ExpectedEdge, ExperimentMeta, Molecule, Peak, Site,
                     Bond)
from .units import parse_quantity, parse_shift

KNOWN_NUCLEI = ("1H", "13C")

_MULT_CANON = {
    "s": "s", "singlet": "s",
    "d": "d", "doublet": "d",
    "t": "t", "triplet": "t",
    "q": "q", "quartet": "q",
    "quint": "quint", "quintet": "quint", "p": "quint",
    "sext": "sext", "sextet": "sext",
    "sept": "sept", "septet": "sept", "h": "sept",
    "m": "m", "multiplet": "m",
    "dd": "dd", "dt": "dt", "dq": "dq", "ddd": "ddd", "ddq": "ddq",
    "br s": "br s", "brs": "br s", "br d": "br d",
}


class IngestError(ValueError):
    def __init__(self, message: str, path: str = "", line: Optional[int] = None):
        loc = path
        if line is not None:
            loc = f"{path}:{line}" if path else f"line {line}"
        super().__init__(f"{loc + ': ' if loc else ''}{message}")
        self.message = message
        self.path = path
        self.line = line


@dataclass
class RevisionContent:
    """One source document at one version, parsed into stable entities."""
    path: str
    kind: str                       # peaks / edges / molecule / meta
    entities: List[dict] = field(default_factory=list)
    meta: Optional[dict] = None    # for kind == "meta"
    diagnostics: List[str] = field(default_factory=list)
    raw: str = ""
    seq: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {"path": self.path, "kind": self.kind, "entities": self.entities,
             "meta": self.meta, "diagnostics": self.diagnostics, "raw": self.raw,
             "seq": self.seq}, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
def _split_lines(text: str) -> List[Tuple[int, str]]:
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith("#"):
            out.append((i, line))
    return out


def _id_tag(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    tag = "".join(ch if ch.isalnum() else "-" for ch in base).strip("-")
    import re
    tag = re.sub(r"-+", "-", tag)
    parts = [p for p in tag.split("-") if p not in ("peaks", "peak", "list")]
    return "-".join(parts)[:24] or "list"


def parse_peaks(path: str, text: str, id_tag: Optional[str] = None) -> RevisionContent:
    rows = _split_lines(text)
    if not rows:
        raise IngestError("empty peak list", path)
    header_line, header_raw = rows[0]
    header = next(csv.reader(io.StringIO(header_raw), skipinitialspace=True))
    header = [h.strip().lower() for h in header]
    required = ["nucleus", "shift", "intensity"]
    for col in required:
        if col not in header:
            raise IngestError(f"peak header missing required column {col!r}",
                              path, header_line)
    allowed = {"nucleus", "shift", "intensity", "integration", "multiplicity",
               "overlap_group", "note"}
    unknown = [c for c in header if c not in allowed]
    if unknown:
        raise IngestError(f"unknown peak columns {unknown}", path, header_line)

    content = RevisionContent(path=path, kind="peaks", raw=text)
    counters = {"1H": 0, "13C": 0}
    for lineno, line in rows[1:]:
        values = next(csv.reader(io.StringIO(line), skipinitialspace=True))
        values = [v.strip() for v in values]
        if len(values) != len(header):
            raise IngestError(
                f"expected {len(header)} columns, got {len(values)}", path, lineno)
        row = dict(zip(header, values))
        nucleus = row["nucleus"]
        if nucleus not in KNOWN_NUCLEI:
            raise IngestError(f"unknown nucleus {nucleus!r} (allowed: {KNOWN_NUCLEI})",
                              path, lineno)
        diagnostics: List[str] = []
        shift = parse_shift(row["shift"])
        if shift.value is None:
            diagnostics.append(f"{path}:{lineno}: shift {shift.diagnostic}")
        intensity = parse_quantity(row["intensity"], default_unit="a.u.")
        if intensity.value is None:
            diagnostics.append(f"{path}:{lineno}: intensity {intensity.diagnostic}")
        integration = None
        if row.get("integration"):
            integration = parse_quantity(row["integration"], default_unit="H")
            if integration.value is None:
                diagnostics.append(
                    f"{path}:{lineno}: integration {integration.diagnostic}")
        mult_raw = row.get("multiplicity") or None
        mult_norm = None
        if mult_raw:
            mult_norm = _MULT_CANON.get(mult_raw.lower())
            if mult_norm is None:
                diagnostics.append(
                    f"{path}:{lineno}: unrecognised multiplicity {mult_raw!r}; "
                    "raw token retained")
        counters[nucleus] += 1
        tag = id_tag or _id_tag(path)
        prefix = "h" if nucleus == "1H" else "c"
        peak = Peak(
            id=f"{prefix}p-{tag}-{counters[nucleus]:03d}", nucleus=nucleus, shift=shift,
            intensity=intensity, integration=integration, multiplicity=mult_raw,
            multiplicity_norm=mult_norm, overlap_group=row.get("overlap_group") or None,
            meta={"note": row["note"]} if row.get("note") else {})
        content.entities.append(peak.to_dict())
        content.diagnostics.extend(diagnostics)
    return content


def parse_edges(path: str, text: str, kind: str,
                known_peaks: Optional[set] = None) -> RevisionContent:
    if kind not in ("COSY", "HSQC", "HMBC"):
        raise IngestError(f"unknown edge kind {kind!r}", path)
    rows = _split_lines(text)
    if not rows:
        raise IngestError("empty edge list", path)
    header_line, header_raw = rows[0]
    header = [h.strip().lower() for h in
              next(csv.reader(io.StringIO(header_raw), skipinitialspace=True))]
    required = ["from_peak", "to_peak"]
    for col in required:
        if col not in header:
            raise IngestError(f"edge header missing column {col!r}", path, header_line)
    content = RevisionContent(path=path, kind="edges", raw=text)
    for lineno, line in rows[1:]:
        values = [v.strip() for v in
                  next(csv.reader(io.StringIO(line), skipinitialspace=True))]
        if len(values) != len(header):
            raise IngestError(
                f"expected {len(header)} columns, got {len(values)}", path, lineno)
        row = dict(zip(header, values))
        strength = parse_quantity(row.get("strength", ""), default_unit="a.u.")
        edge = Edge(
            id=f"{kind.lower()}-{lineno - 1:03d}", kind=kind,
            from_peak=row["from_peak"], to_peak=row["to_peak"], strength=strength,
            meta={k: v for k, v in row.items()
                  if k not in ("from_peak", "to_peak", "strength")})
        d = edge.to_dict()
        diag = None
        if known_peaks is not None:
            for ref in (edge.from_peak, edge.to_peak):
                if ref not in known_peaks:
                    diag = f"{path}:{lineno}: {kind} edge references unknown peak {ref!r}; edge retained"
                    content.diagnostics.append(diag)
        if diag:
            d["diagnostic"] = diag
        content.entities.append(d)
    return content


def parse_molecule(path: str, text: str) -> RevisionContent:
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestError(f"invalid JSON molecule: {exc}", path)
    for key in ("id", "name", "sites", "bonds"):
        if key not in spec:
            raise IngestError(f"molecule document missing key {key!r}", path)
    sites: List[Site] = []
    site_ids = set()
    for i, s in enumerate(spec["sites"], start=1):
        for key in ("id", "nucleus", "label", "expected_shift"):
            if key not in s:
                raise IngestError(f"site #{i} missing key {key!r}", path)
        if s["nucleus"] not in KNOWN_NUCLEI:
            raise IngestError(f"site {s['id']!r} unknown nucleus", path)
        site = Site(
            id=s["id"], nucleus=s["nucleus"], label=s["label"],
            expected_shift=float(s["expected_shift"]),
            expected_mult=s.get("expected_mult"),
            expected_integral=s.get("expected_integral"),
            symmetry_group=s.get("symmetry_group"),
            x=float(s.get("x", 0.0)), y=float(s.get("y", 0.0)))
        if site.id in site_ids:
            raise IngestError(f"duplicate site id {site.id!r}", path)
        site_ids.add(site.id)
        sites.append(site)
    bonds: List[Bond] = []
    for i, b in enumerate(spec["bonds"], start=1):
        for key in ("site_a", "site_b"):
            if key not in b:
                raise IngestError(f"bond #{i} missing key {key!r}", path)
        if b["site_a"] not in site_ids or b["site_b"] not in site_ids:
            raise IngestError(f"bond #{i} references unknown site", path)
        bonds.append(Bond(b["site_a"], b["site_b"], int(b.get("order", 1))))
    expected: List[ExpectedEdge] = []
    for i, e in enumerate(spec.get("expected_edges", []), start=1):
        for key in ("id", "kind", "from_site", "to_site", "bond_distance"):
            if key not in e:
                raise IngestError(f"expected_edge #{i} missing key {key!r}", path)
        if e["kind"] not in ("COSY", "HSQC", "HMBC"):
            raise IngestError(f"expected_edge #{i} unknown kind", path)
        if e["from_site"] not in site_ids or e["to_site"] not in site_ids:
            raise IngestError(f"expected_edge #{i} references unknown site", path)
        expected.append(ExpectedEdge(**e))
    mol = Molecule(id=spec["id"], name=spec["name"], smiles=spec.get("smiles", ""),
                   sites=sites, bonds=bonds, expected_edges=expected)
    return RevisionContent(path=path, kind="molecule", entities=[mol.to_dict()],
                           raw=text)


def parse_meta(path: str, text: str) -> RevisionContent:
    rows = _split_lines(text)
    fields: Dict[str, str] = {}
    diagnostics: List[str] = []
    for lineno, line in rows:
        if "=" not in line:
            raise IngestError("meta lines must be key=value", path, lineno)
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        key_lower = key.lower()
        if key_lower in fields:
            diagnostics.append(f"{path}:{lineno}: duplicate key {key!r}; last wins")
        fields[key_lower] = value
    for required in ("solvent", "reference"):
        if required not in fields:
            raise IngestError(f"meta missing required key {required!r}", path)
    temperature = None
    if "temperature" in fields:
        temperature = parse_quantity(fields["temperature"], default_unit="K")
        if temperature.value is None:
            diagnostics.append(f"{path}: temperature {temperature.diagnostic}")
    frequencies: Dict[str, object] = {}
    freqs_raw: Dict[str, str] = {}
    for key, value in fields.items():
        if key.startswith("frequency."):
            nucleus = key.split(".", 1)[1].upper()
            if nucleus not in KNOWN_NUCLEI:
                diagnostics.append(f"{path}: frequency for unknown nucleus {nucleus!r}")
            q = parse_quantity(value, default_unit="MHz")
            frequencies[nucleus] = q.to_dict()
            freqs_raw[key] = value
            if q.value is None:
                diagnostics.append(f"{path}: {key} {q.diagnostic}")
    known_keys = {"solvent", "temperature", "reference", "note"} | {
        k for k in fields if k.startswith("frequency.")}
    for key in fields:
        if key not in known_keys:
            diagnostics.append(f"{path}: unknown meta key {key!r}; raw retained")
    meta = ExperimentMeta(
        solvent=fields["solvent"], temperature=temperature,
        reference_standard=fields["reference"],
        frequencies={}, raw=fields, diagnostics=diagnostics)
    meta_dict = meta.to_dict()
    meta_dict["frequencies"] = frequencies
    return RevisionContent(path=path, kind="meta", meta=meta_dict,
                           diagnostics=diagnostics, raw=text)


# --------------------------------------------------------------------------
def kind_for_path(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.endswith(".json"):
        return "molecule"
    if name.endswith(".meta") or name.endswith(".env"):
        return "meta"
    if "cosy" in name:
        return "edge:COSY"
    if "hsqc" in name:
        return "edge:HSQC"
    if "hmbc" in name:
        return "edge:HMBC"
    if name.endswith(".csv"):
        return "peaks"
    raise IngestError(f"cannot infer document kind from filename {path!r}", path)


def parse_document(path: str, text: str, kind: Optional[str] = None,
                   known_peaks: Optional[set] = None) -> RevisionContent:
    kind = kind or kind_for_path(path)
    if kind == "peaks":
        return parse_peaks(path, text)
    if kind.startswith("edge:"):
        return parse_edges(path, text, kind.split(":", 1)[1], known_peaks)
    if kind == "molecule":
        return parse_molecule(path, text)
    if kind == "meta":
        return parse_meta(path, text)
    raise IngestError(f"unknown document kind {kind!r}", path)
