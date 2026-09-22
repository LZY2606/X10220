"""Parser for the small copy-pasteable peak-list text format.

Format (line based, '#' comments):

    # nmr-peaklist v1
    experiment: 1H
    solvent: CDCl3
    frequency_mhz: 400.13
    temperature_K: 298.0
    reference: TMS
    peak,shift_ppm,intensity,integral,multiplicity
    H1,1.051,0.82,3.0,t

    # nmr-peaklist v1
    experiment: COSY
    edge,from,to
    CO1,H1,H2

    # nmr-molecule v1
    atom,element,label,shift_min,shift_max,symmetry,x,y,attached_protons
    bond,A,B

Format errors (bad header, unknown experiment, wrong columns, missing
metadata) are reported as errors and the import is rejected. Values that
cannot be converted to numbers/units are NOT errors: they stay raw and get a
diagnostic attached.
"""

from typing import List, Tuple

from .models import (Atom, Bond, Conditions, Edge, EXPERIMENTS_1D,
                     EXPERIMENTS_2D, NUCLEUS_OF, ParsedDoc, Peak)
from .units import parse_integral, parse_intensity, parse_multiplicity, parse_shift

FORMAT_PEAKLIST = "nmr-peaklist v1"
FORMAT_MOLECULE = "nmr-molecule v1"
REQUIRED_META_1D = ["solvent", "frequency_mhz", "temperature_K", "reference"]


def _split_lines(text: str) -> List[Tuple[int, str]]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if line:
            out.append((n, line))
    return out


def parse_document(text: str, source_id: str) -> Tuple[ParsedDoc, List[str]]:
    errors: List[str] = []
    lines = _split_lines(text)
    if not lines:
        return ParsedDoc(kind=""), ["empty document"]
    header = lines[0][1]
    if header == f"# {FORMAT_PEAKLIST}":
        return _parse_peaklist(lines[1:], source_id, errors)
    if header == f"# {FORMAT_MOLECULE}":
        return _parse_molecule(lines[1:], source_id, errors)
    return ParsedDoc(kind=""), [
        f"line 1: unknown format header {header!r}; expected "
        f"'# {FORMAT_PEAKLIST}' or '# {FORMAT_MOLECULE}'"
    ]


def _parse_meta(lines, errors):
    meta, table, header_seen = {}, [], None
    for n, line in lines:
        if line.startswith("#"):
            continue
        if ":" in line and not line.startswith(("peak,", "edge,", "atom,", "bond,")) \
                and header_seen is None and "," not in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
        elif "," in line:
            if header_seen is None:
                header_seen = [c.strip() for c in line.split(",")]
            else:
                table.append((n, [c.strip() for c in line.split(",")]))
        else:
            errors.append(f"line {n}: cannot parse {line!r}")
    return meta, header_seen, table


def _parse_peaklist(lines, source_id, errors):
    doc = ParsedDoc(kind="peaklist")
    meta, header, table = _parse_meta(lines, errors)
    exp = meta.get("experiment", "")
    if not exp:
        errors.append("missing metadata 'experiment'")
        return doc, errors
    if exp not in EXPERIMENTS_1D | EXPERIMENTS_2D:
        errors.append(f"unknown experiment {exp!r}; expected one of "
                      f"{sorted(EXPERIMENTS_1D | EXPERIMENTS_2D)}")
        return doc, errors
    doc.experiment = exp
    if exp in EXPERIMENTS_1D:
        for key in REQUIRED_META_1D:
            if key not in meta:
                errors.append(f"missing metadata '{key}' for 1D experiment {exp}")
        cond = Conditions(
            solvent=meta.get("solvent", ""),
            temperature_K=_float_or_none(meta.get("temperature_K")),
            reference=meta.get("reference", ""),
        )
        try:
            cond.frequency_mhz = float(meta.get("frequency_mhz", ""))
        except ValueError:
            errors.append(f"frequency_mhz {meta.get('frequency_mhz')!r} is not a number")
        doc.conditions = cond
        expected = ["peak", "shift_ppm", "intensity", "integral", "multiplicity"]
        if header != expected:
            errors.append(f"bad table header {header}; expected {expected}")
            return doc, errors
        for n, cols in table:
            if len(cols) != 5:
                errors.append(f"line {n}: expected 5 columns, got {len(cols)}")
                continue
            pid, shift, inten, integ, mult = cols
            peak = Peak(
                id=pid, source_id=source_id, experiment=exp,
                nucleus=NUCLEUS_OF[exp],
                shift=parse_shift(shift),
                intensity=parse_intensity(inten),
                integral=parse_integral(integ),
                multiplicity=parse_multiplicity(mult),
            )
            for q in (peak.shift, peak.intensity, peak.integral):
                if q.diagnostic:
                    doc.diagnostics.append(f"peak {pid}: {q.diagnostic}")
            if peak.multiplicity.diagnostic:
                doc.diagnostics.append(f"peak {pid}: {peak.multiplicity.diagnostic}")
            doc.peaks.append(peak)
    else:
        expected = ["edge", "from", "to"]
        if header != expected:
            errors.append(f"bad table header {header}; expected {expected}")
            return doc, errors
        for n, cols in table:
            if len(cols) != 3:
                errors.append(f"line {n}: expected 3 columns, got {len(cols)}")
                continue
            doc.edges.append(Edge(id=cols[0], source_id=source_id, kind=exp,
                                  from_peak=cols[1], to_peak=cols[2]))
    return doc, errors


def _parse_molecule(lines, source_id, errors):
    doc = ParsedDoc(kind="molecule")
    atoms_header = ["atom", "element", "label", "shift_min", "shift_max",
                    "symmetry", "x", "y", "attached_protons"]
    for n, line in lines:
        if line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split(",")]
        if cols[0] == "atom":
            if cols != atoms_header:
                errors.append(f"line {n}: bad atom header {cols}; expected {atoms_header}")
            continue
        if cols[0] == "bond":
            if len(cols) != 3:
                errors.append(f"line {n}: bond needs 2 atom ids")
                continue
            doc.bonds.append(Bond(a=cols[1], b=cols[2]))
            continue
        if len(cols) != 9:
            errors.append(f"line {n}: atom row needs 9 columns, got {len(cols)}")
            continue
        try:
            doc.atoms.append(Atom(
                id=cols[0], element=cols[1], label=cols[2],
                shift_min=float(cols[3]), shift_max=float(cols[4]),
                symmetry_group=cols[5], x=float(cols[6]), y=float(cols[7]),
                attached_protons=int(cols[8]),
            ))
        except ValueError as exc:
            errors.append(f"line {n}: {exc}")
    if not doc.atoms:
        errors.append("molecule document contains no atoms")
    return doc, errors


def _float_or_none(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
