"""Source-file import layer.

Accepts small copy-pasteable text case files for four roles:
``peaklist`` / ``params`` / ``molecule``.  Original lines are retained on every
record (``raw_line``); normalization to floats/units happens in the domain
layer.  Malformed input is reported per-line (``diagnostics``); an error blocks
commit, a warning does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import units

# (required headers, optional headers, accepted nuclei/sections)
_PEAK_REQUIRED = {"id", "nucleus", "shift", "kind"}
_PARAM_REQUIRED = {"algo_version"}
_MOL_REQUIRED = {"id", "element", "x", "y"}

_MULTIPLICITY = {"s", "d", "t", "q", "m", "br s", "brs", "dd", "dt", "dq"}


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning"
    code: str
    line_no: int
    line: str
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code,
                "line_no": self.line_no, "line": self.line,
                "message": self.message}


@dataclass
class ParsedSource:
    role: str
    format_version: str
    header: dict
    records: list
    raw_text: str
    diagnostics: list = field(default_factory=list)
    lines: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def to_dict(self) -> dict:
        return {"role": self.role, "format_version": self.format_version,
                "header": self.header, "records": self.records,
                "raw_text": self.raw_text,
                "diagnostics": [d.to_dict() for d in self.diagnostics]}


def _kv(line: str) -> tuple[str, str]:
    key, _, value = line.partition(":")
    return key.strip().lower(), value.strip()


def _tokens(line: str) -> list[str]:
    return [t.strip() for t in line.split("|") if t.strip() != ""]


def parse_source(role: str, raw_text: str) -> ParsedSource:
    if role not in ("peaklist", "params", "molecule"):
        raise ValueError(f"unknown source role {role!r}")
    lines = raw_text.splitlines()
    ds: list[Diagnostic] = []

    def err(code, i, msg):
        ds.append(Diagnostic("error", code, i + 1, lines[i], msg))

    def warn(code, i, msg):
        ds.append(Diagnostic("warning", code, i + 1, lines[i], msg))

    if not lines or not lines[0].strip():
        return ParsedSource(role, "0.0", {}, [], raw_text,
                            [Diagnostic("error", "EMPTY_FILE", 0, "",
                                        "source file is empty")])
    tag, version = _kv(lines[0])
    if tag != "nmr-workbench" or not version:
        return ParsedSource(role, "unknown", {}, [], raw_text, [
            Diagnostic("error", "BAD_HEADER", 1, lines[0],
                       "first line must be 'NMR-WORKBENCH: <role>/vN'")])
    expected = {"peaklist": "peaklist", "params": "params",
                "molecule": "molecule"}[role]
    if version != f"{expected}/v1":
        return ParsedSource(role, version, {}, [], raw_text, [
            Diagnostic("error", "BAD_HEADER", 1, lines[0],
                       f"expected 'NMR-WORKBENCH: {expected}/v1'")])

    header: dict = {}
    body_start = 1
    for i in range(1, len(lines)):
        line = lines[i].strip()
        if line.startswith("#") or not line:
            continue
        key, value = _kv(line)
        if key in _SECTION_MARKERS:
            body_start = i
            break
        header[key] = {"value": value, "raw_line": line, "line_no": i + 1}

    # role-specific header validation
    if role == "peaklist":
        for need in ("solvent", "frequency"):
            if need not in header:
                err("MISSING_HEADER", body_start,
                    f"peaklist header must declare '{need}'")
        if "frequency" in header:
            q = units.parse_frequency(header["frequency"]["value"])
            if q.diagnostic:
                err("BAD_FREQUENCY", header["frequency"]["line_no"] - 1,
                    q.diagnostic)
        if "temperature" in header:
            q = units.parse_temperature(header["temperature"]["value"])
            if q.diagnostic:
                err("BAD_TEMPERATURE", header["temperature"]["line_no"] - 1,
                    q.diagnostic)
    records: list = []
    if role == "peaklist":
        records = _parse_peaklist(lines, body_start, ds, err, warn)
    elif role == "params":
        records, header = _parse_params(lines, body_start, header, ds, err, warn)
    else:
        records = _parse_molecule(lines, body_start, ds, err, warn)

    if role == "params" and "algo_version" not in header:
        err("MISSING_HEADER", 0, "params must declare 'algo_version'")
    if role == "molecule":
        atom_ids = [r["id"] for r in records if r["kind"] == "atom"]
        for need in ("C1",):
            if need not in atom_ids:
                err("MISSING_ATOM", 0, f"molecule must define atom {need}")
    return ParsedSource(role, version, header, records, raw_text, ds, lines)


_SECTION_MARKERS = {"section", "atoms", "bonds", "equivalents", "priors"}


def _section_name(line: str) -> str | None:
    key, value = _kv(line)
    if key == "section":
        return value.strip().lower()
    return None


def _parse_peaklist(lines, body_start, ds, err, warn):
    section = "peaks"
    records = []
    seen_ids = set()
    peak_cols = ("id", "nucleus", "shift", "intensity", "integral", "multiplicity")
    for i in range(body_start, len(lines)):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            continue
        key, value = _kv(line)
        if key == "section":
            section = value.strip().lower()
            if section not in ("peaks", "cosy", "hsqc", "hmbc"):
                err("BAD_SECTION", i, f"unknown section {value!r}")
            continue
        toks = _tokens(line)
        if section == "peaks":
            if len(toks) < 3:
                err("BAD_PEAK", i, "peak rows need id|nucleus|shift fields")
                continue
            rec = dict(zip(peak_cols, toks))
            rec.update({"kind": "peak", "section": "peaks",
                        "raw_line": line, "line_no": i + 1,
                        "extra_fields": toks[len(peak_cols):]})
            if rec["id"] in seen_ids:
                err("DUP_ID", i, f"duplicate peak id {rec['id']!r}")
            seen_ids.add(rec["id"])
            nuc = rec.get("nucleus", "").strip()
            if nuc not in ("1H", "13C"):
                err("BAD_NUCLEUS", i, f"nucleus must be 1H or 13C, got {nuc!r}")
            q = units.parse_shift(rec["shift"])
            if q.diagnostic:
                err("BAD_SHIFT", i, q.diagnostic)
            rec["shift_q"] = q.to_dict()
            if "intensity" in rec:
                qi = units.parse_intensity(rec["intensity"])
                if qi.diagnostic:
                    warn("BAD_INTENSITY", i, qi.diagnostic)
                rec["intensity_q"] = qi.to_dict()
            if "integral" in rec:
                qn = units.parse_integral(rec["integral"])
                if qn.diagnostic:
                    warn("BAD_INTEGRAL", i, qn.diagnostic)
                rec["integral_q"] = qn.to_dict()
            if rec.get("multiplicity") and rec["multiplicity"] not in _MULTIPLICITY:
                warn("BAD_MULTIPLICITY", i,
                     f"unrecognized multiplicity {rec['multiplicity']!r}; kept")
            records.append(rec)
        else:
            # correlation edges: id | shift1 | shift2 [ | note ]
            if len(toks) < 3:
                err("BAD_EDGE", i, f"{section} rows need id|shift1|shift2")
                continue
            edge_id, s1, s2 = toks[0], toks[1], toks[2]
            if edge_id in seen_ids:
                err("DUP_ID", i, f"duplicate edge id {edge_id!r}")
            seen_ids.add(edge_id)
            q1, q2 = units.parse_shift(s1), units.parse_shift(s2)
            if q1.diagnostic:
                err("BAD_SHIFT", i, f"edge shift1: {q1.diagnostic}")
            if q2.diagnostic:
                err("BAD_SHIFT", i, f"edge shift2: {q2.diagnostic}")
            records.append({
                "kind": "edge", "section": section, "id": edge_id,
                "shift1": s1, "shift2": s2,
                "shift1_q": q1.to_dict(), "shift2_q": q2.to_dict(),
                "note": toks[3] if len(toks) > 3 else None,
                "raw_line": line, "line_no": i + 1})
    return records


def _parse_params(lines, body_start, header, ds, err, warn):
    # params files use flat key: value lines in addition to the header block
    records = []
    for i in range(body_start, len(lines)):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            continue
        key, value = _kv(line)
        if key in _SECTION_MARKERS:
            continue
        prev = header.get(key)
        header[key] = {"value": value, "raw_line": line, "line_no": i + 1}
        records.append({"key": key, "value": value, "raw_line": line,
                        "line_no": i + 1, "overrides_header": prev is not None})
    return records, header


def _parse_molecule(lines, body_start, ds, err, warn):
    section = "atoms"
    records = []
    atom_ids = set()
    for i in range(body_start, len(lines)):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            continue
        key, value = _kv(line)
        if key == "section":
            section = value.strip().lower()
            if section not in ("atoms", "bonds", "equivalents", "priors"):
                err("BAD_SECTION", i, f"unknown section {value!r}")
            continue
        toks = _tokens(line)
        if section == "atoms":
            if len(toks) < 4:
                err("BAD_ATOM", i, "atom rows need id|element|x|y")
                continue
            aid, elem, x, y = toks[:4]
            if aid in atom_ids:
                err("DUP_ID", i, f"duplicate atom id {aid!r}")
            atom_ids.add(aid)
            try:
                xn, yn = float(x), float(y)
            except ValueError:
                err("BAD_COORD", i, f"atom {aid}: coordinates must be numbers")
                xn = yn = None
            if elem not in ("H", "C", "O", "N", "D"):
                warn("BAD_ELEMENT", i, f"unusual element {elem!r}; kept")
            rec = {"kind": "atom", "id": aid, "element": elem, "x": xn,
                   "y": yn, "label": toks[4] if len(toks) > 4 else None,
                   "exchangeable": False, "raw_line": line, "line_no": i + 1}
            if len(toks) > 5 and "exchangeable" in toks[5].lower():
                rec["exchangeable"] = True
            records.append(rec)
        elif section == "bonds":
            if len(toks) < 2:
                err("BAD_BOND", i, "bond rows need atom1|atom2")
                continue
            records.append({"kind": "bond", "a1": toks[0], "a2": toks[1],
                            "order": toks[2] if len(toks) > 2 else "1",
                            "raw_line": line, "line_no": i + 1})
        elif section == "equivalents":
            # label | kind(H|C) | atom ids...
            if len(toks) < 3:
                err("BAD_EQUIV", i, "equivalent rows need label|kind|atoms...")
                continue
            records.append({"kind": "equivalent", "label": toks[0],
                            "nucleus": toks[1], "atoms": toks[2:],
                            "raw_line": line, "line_no": i + 1})
        else:  # priors: site_label | nucleus | shift | reference
            if len(toks) < 3:
                err("BAD_PRIOR", i, "prior rows need label|nucleus|shift")
                continue
            q = units.parse_shift(toks[2])
            if q.diagnostic:
                err("BAD_SHIFT", i, f"prior: {q.diagnostic}")
            records.append({"kind": "prior", "site": toks[0],
                            "nucleus": toks[1], "shift": toks[2],
                            "shift_q": q.to_dict(),
                            "reference": toks[3] if len(toks) > 3 else "literature",
                            "raw_line": line, "line_no": i + 1})
    # validate bonds/equivalents refer to declared atoms
    for rec in records:
        if rec["kind"] == "bond":
            for a in (rec["a1"], rec["a2"]):
                if a not in atom_ids:
                    err("DANGLING_BOND", rec["line_no"] - 1,
                        f"bond references unknown atom {a!r}")
        if rec["kind"] == "equivalent":
            for a in rec["atoms"]:
                if a not in atom_ids:
                    err("DANGLING_EQUIV", rec["line_no"] - 1,
                        f"equivalent group references unknown atom {a!r}")
    return records
