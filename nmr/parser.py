"""Parser for the team's copy-pasteable NMR peak-list text format.

The format is line oriented; fields inside a line are separated by ``;`` so
that values can contain spaces (e.g. a multiplicity ``br s``)::

    # comments start with '#'
    @project 3-methyl-2-butanol
    @version 1
    @solvent CDCl3
    @temperature 298K
    @reference TMS
    @spectrometer 400MHz
    @spectrum 1H ppm
      p1 ; 1.28ppm ; 9200a.u. ; 3 ; d ; J=7.0Hz
    @edges HSQC ppm,ppm
      p1 ; cp1
    @edges COSY ppm,ppm
      p1 ; p2
    @edges HMBC ppm,ppm
      p2 ; cp3
    @structure
      site H1 ; element=H ; expected=0.95ppm ; mult=d ; integral=3 ; group=g1 ; attached=C1
      bond ; C1 ; C2

Only validation happens here; no normalization for computation. Every
logical line becomes a *fragment* with a stable (block, ordinal) key so that
later versions can be diffed at the fragment level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .units import parse_quantity

HEADER_KEYS = ("project", "version", "solvent", "temperature", "reference", "spectrometer")
EDGE_KINDS = ("HSQC", "COSY", "HMBC")


@dataclass
class Diagnostic:
    severity: str  # 'error' | 'warning'
    line: int
    code: str
    message: str
    context: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "line": self.line,
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


@dataclass
class Fragment:
    key: Tuple[Any, ...]
    ordinal: int
    raw: str
    block: str

    def to_dict(self) -> dict:
        return {"key": list(self.key), "ordinal": self.ordinal, "raw": self.raw, "block": self.block}


@dataclass
class Peak:
    id: str
    ordinal: int
    spectrum: str
    shift: Any
    intensity: Any
    integral: Any
    multiplicity: Optional[str]
    attrs: Dict[str, str]
    raw: str


@dataclass
class Edge:
    kind: str
    ordinal: int
    a: str
    b: str
    attrs: Dict[str, str]
    raw: str


@dataclass
class Spectrum:
    nucleus: str
    unit: Optional[str]
    peaks: List[Peak] = field(default_factory=list)


@dataclass
class Site:
    id: str
    element: Optional[str]
    expected: Any
    multiplicity: Optional[str]
    integral: Any
    group: Optional[str]
    attached: Optional[str]
    attrs: Dict[str, str]
    raw: str


@dataclass
class Bond:
    ordinal: int
    a: str
    b: str
    raw: str


@dataclass
class ExpectedCorr:
    ordinal: int
    a: str
    b: str
    kind: str
    raw: str


@dataclass
class ParsedDocument:
    name: str
    headers: Dict[str, str]
    spectra: Dict[str, Spectrum]
    edges: Dict[str, List[Edge]]
    sites: List[Site]
    bonds: List[Bond]
    corrs: List[ExpectedCorr]
    fragments: List[Fragment]
    diagnostics: List[Diagnostic]

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "headers": dict(self.headers),
            "spectra": {
                nuc: {
                    "nucleus": s.nucleus,
                    "unit": s.unit,
                    "peaks": [
                        {
                            "id": p.id,
                            "ordinal": p.ordinal,
                            "shift": p.shift.to_dict(),
                            "intensity": p.intensity.to_dict(),
                            "integral": p.integral.to_dict(),
                            "multiplicity": p.multiplicity,
                            "attrs": dict(p.attrs),
                            "raw": p.raw,
                        }
                        for p in s.peaks
                    ],
                }
                for nuc, s in self.spectra.items()
            },
            "edges": {
                kind: [
                    {"ordinal": e.ordinal, "a": e.a, "b": e.b, "attrs": dict(e.attrs), "raw": e.raw}
                    for e in edges
                ]
                for kind, edges in self.edges.items()
            },
            "sites": [
                {
                    "id": s.id,
                    "element": s.element,
                    "expected": s.expected.to_dict() if s.expected is not None else None,
                    "multiplicity": s.multiplicity,
                    "integral": s.integral.to_dict(),
                    "group": s.group,
                    "attached": s.attached,
                    "attrs": dict(s.attrs),
                    "raw": s.raw,
                }
                for s in self.sites
            ],
            "bonds": [{"ordinal": b.ordinal, "a": b.a, "b": b.b, "raw": b.raw} for b in self.bonds],
            "corrs": [
                {"ordinal": c.ordinal, "a": c.a, "b": c.b, "kind": c.kind, "raw": c.raw}
                for c in self.corrs
            ],
            "fragments": [f.to_dict() for f in self.fragments],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _fields(line: str) -> List[str]:
    return [f.strip() for f in line.split(";")]


def _kv(token: str) -> Tuple[str, str]:
    if "=" not in token:
        raise ValueError("expected key=value, got %r" % token)
    k, v = token.split("=", 1)
    return k.strip().lower(), v.strip()


def parse_document(name: str, text: str) -> ParsedDocument:
    diags: List[Diagnostic] = []

    def err(line_no, code, message, context):
        diags.append(Diagnostic("error", line_no, code, message, context))

    def warn(line_no, code, message, context):
        diags.append(Diagnostic("warning", line_no, code, message, context))

    headers: Dict[str, str] = {}
    spectra: Dict[str, Spectrum] = {}
    edges: Dict[str, List[Edge]] = {k: [] for k in EDGE_KINDS}
    sites: List[Site] = []
    bonds: List[Bond] = []
    corrs: List[ExpectedCorr] = []
    fragments: List[Fragment] = []

    section: Optional[str] = None  # ('spectrum', nucleus) | ('edges', kind) | 'structure'
    seen_peak_ids: Dict[str, str] = {}
    seen_site_ids = set()
    ord_counters: Dict[tuple, int] = {}

    def next_ord(kind):
        ord_counters[kind] = ord_counters.get(kind, 0) + 1
        return ord_counters[kind]

    raw_lines = text.splitlines()
    for idx, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        norm = ";".join(f.strip() for f in line.split(";"))
        if line.startswith("@"):
            head = line[1:]
            key, _, value = head.partition(" ")
            key = key.strip().lower()
            value = value.strip()
            if key == "spectrum":
                parts = value.split()
                if len(parts) < 1 or parts[0] not in ("1H", "13C"):
                    err(idx, "bad_spectrum", "spectrum must name 1H or 13C", line)
                    section = None
                    continue
                nuc = parts[0]
                unit = parts[1] if len(parts) > 1 else None
                if nuc in spectra:
                    err(idx, "dup_spectrum", "spectrum %s declared twice" % nuc, line)
                spectra.setdefault(nuc, Spectrum(nuc, unit))
                section = ("spectrum", nuc)
                ord_counters[("peak", nuc)] = 0
            elif key == "edges":
                parts = value.split()
                kind = parts[0].upper() if parts else ""
                if kind not in EDGE_KINDS:
                    err(idx, "bad_edge_kind", "edge kind must be HSQC/COSY/HMBC", line)
                    section = None
                    continue
                section = ("edges", kind)
                ord_counters[("edge", kind)] = 0
            elif key == "structure":
                section = "structure"
            elif key in HEADER_KEYS:
                if key in headers:
                    warn(idx, "dup_header", "header %s repeated; first value kept" % key, line)
                else:
                    headers[key] = value
                fragments.append(Fragment(("header", key), next_ord(("header",)), norm, "header"))
            else:
                err(idx, "unknown_directive", "unknown directive @%s" % key, line)
            continue

        if section is None:
            err(idx, "outside_section", "data line outside any @spectrum/@edges/@structure", line)
            continue

        if section[0] == "spectrum":
            nuc = section[1]
            fs = _fields(line)
            if len(fs) < 2:
                err(idx, "bad_peak", "peak needs at least id and shift", line)
                continue
            pid = fs[0]
            if not pid:
                err(idx, "bad_peak", "peak id is empty", line)
                continue
            if pid in seen_peak_ids:
                err(idx, "dup_peak", "peak id %r already used in %s" % (pid, seen_peak_ids[pid]), line)
            seen_peak_ids[pid] = nuc
            shift = parse_quantity(fs[1] if len(fs) > 1 else None, "ppm")
            if not shift.numeric:
                err(idx, "bad_shift", "shift not numeric; raw text retained", line)
            intensity = parse_quantity(fs[2] if len(fs) > 2 else None)
            integral = parse_quantity(fs[3] if len(fs) > 3 else None)
            mult = fs[4] if len(fs) > 4 and fs[4] else None
            attrs: Dict[str, str] = {}
            for tok in fs[5:]:
                if not tok:
                    continue
                try:
                    k, v = _kv(tok)
                except ValueError:
                    err(idx, "bad_attr", "peak attribute must be key=value", line)
                    continue
                attrs[k] = v
            ordno = next_ord(("peak", nuc))
            spectra.setdefault(nuc, Spectrum(nuc, None)).peaks.append(
                Peak(pid, ordno, nuc, shift, intensity, integral, mult, attrs, norm)
            )
            fragments.append(Fragment(("peak", nuc, ordno), ordno, norm, "peak:%s" % nuc))
            continue

        if section[0] == "edges":
            kind = section[1]
            fs = _fields(line)
            if len(fs) < 2 or not fs[0] or not fs[1]:
                err(idx, "bad_edge", "edge needs two peak ids", line)
                continue
            attrs = {}
            for tok in fs[2:]:
                if not tok:
                    continue
                try:
                    k, v = _kv(tok)
                except ValueError:
                    err(idx, "bad_attr", "edge attribute must be key=value", line)
                    continue
                attrs[k] = v
            ordno = next_ord(("edge", kind))
            edges[kind].append(Edge(kind, ordno, fs[0], fs[1], attrs, norm))
            fragments.append(Fragment(("edge", kind, ordno), ordno, norm, "edge:%s" % kind))
            continue

        # structure section
        fs = _fields(line)
        kind = fs[0].lower()
        if kind == "site":
            if len(fs) < 2 or not fs[1]:
                err(idx, "bad_site", "site needs an id", line)
                continue
            sid = fs[1]
            if sid in seen_site_ids:
                err(idx, "dup_site", "site id %r repeated" % sid, line)
            seen_site_ids.add(sid)
            attrs = {}
            for tok in fs[2:]:
                if not tok:
                    continue
                try:
                    k, v = _kv(tok)
                except ValueError:
                    err(idx, "bad_attr", "site attribute must be key=value", line)
                    continue
                attrs[k] = v
            element = attrs.pop("element", None)
            if element is None:
                err(idx, "site_no_element", "site %s missing element=" % sid, line)
            expected_raw = attrs.pop("expected", None)
            expected = parse_quantity(expected_raw, "ppm") if expected_raw is not None else None
            if expected is not None and not expected.numeric:
                err(idx, "bad_expected", "expected shift not numeric; raw retained", line)
            mult = attrs.pop("mult", None)
            integral = parse_quantity(attrs.pop("integral", None))
            group = attrs.pop("group", None)
            attached = attrs.pop("attached", None)
            sites.append(Site(sid, element, expected, mult, integral, group, attached, attrs, norm))
            ordno = next_ord(("site",))
            fragments.append(Fragment(("site", sid), ordno, norm, "site"))
        elif kind == "bond":
            if len(fs) < 3 or not fs[1] or not fs[2]:
                err(idx, "bad_bond", "bond needs two site ids", line)
                continue
            ordno = next_ord(("bond",))
            bonds.append(Bond(ordno, fs[1], fs[2], norm))
            fragments.append(Fragment(("bond", ordno), ordno, norm, "bond"))
        elif kind == "corr":
            if len(fs) < 3 or not fs[1] or not fs[2]:
                err(idx, "bad_corr", "corr needs two site ids", line)
                continue
            cattrs = {}
            for tok in fs[3:]:
                if not tok:
                    continue
                try:
                    k, v = _kv(tok)
                except ValueError:
                    err(idx, "bad_attr", "corr attribute must be key=value", line)
                    continue
                cattrs[k] = v
            ck = (cattrs.get("kind") or "").upper()
            if ck not in EDGE_KINDS:
                err(idx, "bad_corr_kind", "corr needs kind=HSQC/COSY/HMBC", line)
                continue
            cord = next_ord(("corr",))
            corrs.append(ExpectedCorr(cord, fs[1], fs[2], ck, norm))
            fragments.append(Fragment(("corr", cord), cord, norm, "corr"))
        else:
            err(idx, "bad_structure_line", "structure line must start with site/bond", line)

    # cross-reference checks (fragments already kept; diagnostics only)
    for nuc, spec in spectra.items():
        if spec.unit not in (None, "ppm"):
            warn(0, "unit_note", "spectrum %s unit declared as %s" % (nuc, spec.unit), "")
    h_peaks = {p.id for p in spectra.get("1H", Spectrum("1H", None)).peaks}
    c_peaks = {p.id for p in spectra.get("13C", Spectrum("13C", None)).peaks}
    for e in edges["COSY"]:
        for ref in (e.a, e.b):
            if ref not in h_peaks:
                err(0, "edge_ref", "COSY edge references unknown 1H peak %r" % ref, e.raw)
    for e in edges["HSQC"] + edges["HMBC"]:
        if e.a not in h_peaks:
            err(0, "edge_ref", "%s edge references unknown 1H peak %r" % (e.kind, e.a), e.raw)
        if e.b not in c_peaks:
            err(0, "edge_ref", "%s edge references unknown 13C peak %r" % (e.kind, e.b), e.raw)
    site_ids = {s.id for s in sites}
    for s in sites:
        if s.attached and s.attached not in site_ids:
            err(0, "attached_ref", "site %s attached to unknown site %r" % (s.id, s.attached), s.raw)
    for b in bonds:
        for ref in (b.a, b.b):
            if ref not in site_ids:
                err(0, "bond_ref", "bond references unknown site %r" % ref, b.raw)

    for c in corrs:
        for ref in (c.a, c.b):
            if ref not in site_ids:
                err(0, "corr_ref", "%s corr references unknown site %r" % (c.kind, ref), c.raw)

    # set fragments: their raw text changes when a peak/edge is added or removed,
    # so missing-peak / network-evidence results can be invalidated locally
    for nuc, spec in spectra.items():
        ids = ",".join(pp.id for pp in spec.peaks)
        fragments.append(Fragment(("spectrum", nuc), next_ord(("spectrum_set",)), ids, "set:spectrum"))
    for kind in EDGE_KINDS:
        raw_repr = ",".join("%s:%s" % (e.a, e.b) for e in edges[kind])
        fragments.append(
            Fragment(("edges", kind), next_ord(("edge_set",)), raw_repr, "set:edges")
        )

    for required in ("project", "solvent", "spectrometer"):
        if required not in headers:
            warn(0, "missing_header", "recommended header @%s absent" % required, "")

    return ParsedDocument(name, headers, spectra, edges, sites, bonds, corrs, fragments, diags)
