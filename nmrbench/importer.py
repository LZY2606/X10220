"""Import layer for small, copy-pasteable NMR text documents.

Design contract
---------------
* Every original line is retained verbatim; normalisation happens only in a
  derived layer (``raw`` is always the authority).
* Quantities carry their unit / reference frame.  Values that cannot be
  converted stay in place with a diagnostic instead of being dropped.
* Hard format errors are raised as :class:`ImportFormatError` with precise
  section / line coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ImportFormatError
from .fingerprint import sha256_short

MAGIC = "@nmr-bench/1"

SPECTRUM_KINDS = {"proton", "carbon", "hsqc", "cosy", "hmbc"}
ALL_KINDS = SPECTRUM_KINDS | {"molecule"}

# header key -> (canonical unit, si unit)
QUANTITY_HEADERS = {
    "frequency_hz": ("Hz", "Hz"),
    "temperature_k": ("K", "K"),
}
REQUIRED_HEADERS = {
    "proton": ("solvent",),
    "carbon": ("solvent",),
    "hsqc": ("solvent",),
    "cosy": ("solvent",),
    "hmbc": ("solvent",),
}

# Unit conversion to SI/base unit.
_UNIT_FACTORS = {
    "ppm": (1.0, "ppm"),
    "hz": (1.0, "Hz"),
    "khz": (1.0e3, "Hz"),
    "mhz": (1.0e6, "Hz"),
    "k": (1.0, "K"),
}

_MULTIPLICITY_TOKENS = {"s", "d", "t", "q", "m", "dd", "dt", "td", "ddd",
                        "dq", "br s", "brs", "sept", "quint"}


@dataclass
class ParsedDoc:
    kind: str
    headers: dict[str, dict[str, Any]]
    header_order: list[str]
    rows: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    raw_text: str
    raw_lines: list[str]
    content_hash: str
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def header_value(self, key: str) -> Any:
        h = self.headers.get(key)
        return h["normalized"] if h else None

    def header_raw(self, key: str) -> str | None:
        h = self.headers.get(key)
        return h["raw"] if h else None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "headers": self.headers,
            "header_order": self.header_order,
            "rows": self.rows,
            "sections": self.sections,
            "raw_text": self.raw_text,
            "content_hash": self.content_hash,
            "diagnostics": self.diagnostics,
        }


def _diag(severity: str, code: str, message: str, *, line: int | None = None,
          section: str | None = None, raw: str | None = None) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message,
            "line": line, "section": section, "raw": raw}


def parse_quantity(raw: str) -> dict[str, Any]:
    """Parse ``"<number> <unit>"`` keeping the original representation.

    Returns raw, numeric value (in the given unit), unit, si-normalised value
    and an optional diagnostic when conversion is impossible.
    ``-`` / empty means "no value".
    """
    text = raw.strip()
    out: dict[str, Any] = {"raw": raw, "value": None, "unit": None,
                           "si_value": None, "diagnostic": None}
    if text in ("", "-", "none", "NA", "n/a"):
        return out
    parts = text.split()
    num_token = parts[0]
    unit = parts[1] if len(parts) > 1 else None
    try:
        value = float(num_token)
    except ValueError:
        out["diagnostic"] = _diag(
            "warning", "unconvertible-value",
            f"value {num_token!r} cannot be converted to a number; kept at "
            "original position")
        return out
    out["value"] = value
    if unit:
        key = unit.lower()
        if key in _UNIT_FACTORS:
            factor, si_unit = _UNIT_FACTORS[key]
            out["unit"] = unit
            out["si_value"] = value * factor
            out["si_unit"] = si_unit
        else:
            out["unit"] = unit
            out["diagnostic"] = _diag(
                "warning", "unknown-unit",
                f"unit {unit!r} is not recognised; value retained raw")
    return out


def _split_row(line: str) -> list[str]:
    # whitespace-separated tokens; the trailing "note/tag" field may contain
    # spaces and is re-joined by callers that expect one.
    return line.split()


def parse_doc(filename: str, text: str) -> ParsedDoc:
    raw_lines = text.splitlines()
    content_hash = sha256_short(text)

    idx = 0
    while idx < len(raw_lines) and (not raw_lines[idx].strip()
                                    or raw_lines[idx].lstrip().startswith("//")):
        idx += 1
    if idx >= len(raw_lines) or not raw_lines[idx].startswith(MAGIC):
        raise ImportFormatError(
            f"first line must be {MAGIC!r} with a kind= argument",
            file=filename, line=idx + 1 if idx < len(raw_lines) else None,
            raw=raw_lines[idx] if idx < len(raw_lines) else None)

    magic_tokens = raw_lines[idx].split()
    kind = None
    for tok in magic_tokens[1:]:
        if tok.startswith("kind="):
            kind = tok.split("=", 1)[1]
    if kind not in ALL_KINDS:
        raise ImportFormatError(
            f"unknown or missing kind (got {kind!r}); expected one of "
            f"{sorted(ALL_KINDS)}", file=filename, line=idx + 1,
            raw=raw_lines[idx])
    first_line = idx + 1
    idx += 1

    headers: dict[str, dict[str, Any]] = {}
    header_order: list[str] = []
    diagnostics: list[dict[str, Any]] = []

    # ---- header block until a lone '---' separator ----
    while idx < len(raw_lines):
        line = raw_lines[idx]
        stripped = line.strip()
        if stripped == "---":
            idx += 1
            break
        if stripped.startswith("//") or not stripped:
            idx += 1
            continue
        if stripped.startswith("##") or stripped.startswith("@"):
            raise ImportFormatError("header block must end with '---'",
                                    file=filename, line=idx + 1, raw=line)
        if ":" not in line:
            raise ImportFormatError("header entry must be 'key: value'",
                                    file=filename, line=idx + 1, raw=line)
        key, raw_val = line.split(":", 1)
        key = key.strip()
        raw_val_s = raw_val.strip()
        if key in headers:
            diagnostics.append(_diag("warning", "duplicate-header",
                                     f"duplicate header {key!r}; first value kept",
                                     line=idx + 1, raw=line))
            idx += 1
            continue
        entry: dict[str, Any] = {"raw": raw_val_s, "normalized": raw_val_s,
                                 "line": idx + 1, "diagnostic": None}
        if key in QUANTITY_HEADERS:
            q = parse_quantity(raw_val_s)
            entry.update({"raw": q["raw"], "normalized": q["si_value"],
                          "unit": q.get("si_unit"), "value": q["value"],
                          "diagnostic": q["diagnostic"]})
            if q["diagnostic"]:
                d = dict(q["diagnostic"], line=idx + 1)
                diagnostics.append(d)
        headers[key] = entry
        header_order.append(key)
        idx += 1
    else:
        raise ImportFormatError("missing '---' separator after header block",
                                file=filename)

    for req in REQUIRED_HEADERS.get(kind, ()):
        if req not in headers:
            raise ImportFormatError(f"required header {req!r} is missing",
                                    file=filename)
    if kind in SPECTRUM_KINDS and "reference" not in headers:
        # Default reference frame is recorded explicitly so downstream always
        # has a coordinate basis.
        headers["reference"] = {"raw": "TMS=0 (default)", "normalized": "TMS=0",
                                "line": None, "diagnostic": None, "default": True}
        header_order.append("reference")

    def make_row(lineno: int, raw: str) -> dict[str, Any]:
        return {"lineno": lineno, "raw": raw.strip(),
                "row_id": f"row_{content_hash[:8]}_{lineno}",
                "diagnostics": []}

    rows: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = [{"name": "main", "start_line": idx + 1}]
    current = "main"
    known_atoms: set[str] = set()

    while idx < len(raw_lines):
        line = raw_lines[idx]
        lineno = idx + 1
        stripped = line.strip()
        idx += 1
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.startswith("##"):
            if kind != "molecule":
                raise ImportFormatError(f"sections are only valid in molecule docs",
                                        file=filename, line=lineno, raw=line)
            current = stripped[2:].strip().lower()
            if current != "bonds":
                raise ImportFormatError(f"unknown section {current!r}",
                                        file=filename, line=lineno, raw=line)
            sections.append({"name": current, "start_line": lineno})
            continue

        tokens = _split_row(stripped)
        row = make_row(lineno, stripped)

        if kind in ("proton", "carbon"):
            if len(tokens) < 4:
                raise ImportFormatError(
                    f"{kind} rows need >=4 fields: shift multiplicity "
                    "intensity integral [note]", file=filename, line=lineno,
                    raw=line)
            shift = parse_quantity(tokens[0])
            if shift["diagnostic"]:
                row["diagnostics"].append(shift["diagnostic"])
                diagnostics.append({**shift["diagnostic"], "line": lineno})
            if shift["value"] is not None and not shift["unit"]:
                shift["unit"] = "ppm"
            mult = tokens[1]
            if mult.lower() not in _MULTIPLICITY_TOKENS:
                d = _diag("info", "unknown-multiplicity",
                          f"multiplicity {mult!r} kept verbatim; not used in "
                          "scoring", line=lineno, raw=line)
                row["diagnostics"].append(d)
                diagnostics.append(d)
            try:
                intensity = float(tokens[2])
                integral = float(tokens[3])
            except ValueError as exc:
                raise ImportFormatError(
                    f"intensity/integral must be numeric: {exc}",
                    file=filename, line=lineno, raw=line) from exc
            row.update({"kind": kind, "section": current,
                        "shift": shift, "multiplicity": mult,
                        "intensity": intensity, "integral": integral,
                        "note": " ".join(tokens[4:])})

        elif kind in ("hsqc", "cosy", "hmbc"):
            if len(tokens) < 3:
                raise ImportFormatError(
                    f"{kind} rows need >=3 fields: f1 f2 intensity [note]",
                    file=filename, line=lineno, raw=line)
            f1 = parse_quantity(tokens[0])
            f2 = parse_quantity(tokens[1])
            for q in (f1, f2):
                if q["diagnostic"]:
                    row["diagnostics"].append(q["diagnostic"])
                    diagnostics.append({**q["diagnostic"], "line": lineno})
                if q["value"] is not None and not q["unit"]:
                    q["unit"] = "ppm"
            try:
                intensity = float(tokens[2])
            except ValueError as exc:
                raise ImportFormatError(f"intensity must be numeric: {exc}",
                                        file=filename, line=lineno,
                                        raw=line) from exc
            row.update({"kind": kind, "section": current,
                        "f1": f1, "f2": f2, "intensity": intensity,
                        "note": " ".join(tokens[3:])})

        elif kind == "molecule" and current == "main":
            if len(tokens) < 6:
                raise ImportFormatError(
                    "atom rows need >=6 fields: id element shift unit "
                    "hydrogens equiv [tag]", file=filename, line=lineno,
                    raw=line)
            atom_id, element = tokens[0], tokens[1]
            if atom_id in known_atoms:
                raise ImportFormatError(f"duplicate atom id {atom_id!r}",
                                        file=filename, line=lineno, raw=line)
            known_atoms.add(atom_id)
            shift = parse_quantity(" ".join(tokens[2:4]))
            if shift["diagnostic"]:
                row["diagnostics"].append(shift["diagnostic"])
                diagnostics.append({**shift["diagnostic"], "line": lineno})
            try:
                hydrogens = int(tokens[4])
            except ValueError as exc:
                raise ImportFormatError("hydrogens must be an integer",
                                        file=filename, line=lineno,
                                        raw=line) from exc
            equiv = tokens[5]
            if equiv in ("none", "-", "."):
                equiv = None
            row.update({"kind": "atom", "section": current,
                        "atom_id": atom_id, "element": element,
                        "shift_hint": shift, "hydrogens": hydrogens,
                        "equiv_of": equiv, "tag": " ".join(tokens[6:])})

        elif kind == "molecule" and current == "bonds":
            if len(tokens) < 2:
                raise ImportFormatError("bond rows need: a b [order] [tag]",
                                        file=filename, line=lineno, raw=line)
            a, b = tokens[0], tokens[1]
            order = 1
            tag = ""
            rest = tokens[2:]
            if rest:
                try:
                    order = int(rest[0])
                    tag = " ".join(rest[1:])
                except ValueError:
                    tag = " ".join(rest)
            row.update({"kind": "bond", "section": current, "a": a, "b": b,
                        "order": order, "tag": tag})

        rows.append(row)

    if kind == "molecule":
        atom_ids = {r["atom_id"] for r in rows if r["kind"] == "atom"}
        for r in rows:
            if r["kind"] != "atom":
                continue
            if r["equiv_of"] is not None and r["equiv_of"] not in atom_ids:
                raise ImportFormatError(
                    f"equiv_of {r['equiv_of']!r} does not reference a known atom",
                    file=filename, line=r["lineno"], raw=r["raw"])
        for r in rows:
            if r["kind"] != "bond":
                continue
            for end in (r["a"], r["b"]):
                if end not in atom_ids:
                    raise ImportFormatError(
                        f"bond endpoint {end!r} is not a known atom",
                        file=filename, line=r["lineno"], raw=r["raw"])

    return ParsedDoc(kind=kind, headers=headers, header_order=header_order,
                     rows=rows, sections=sections, raw_text=text,
                     raw_lines=raw_lines, content_hash=content_hash,
                     diagnostics=diagnostics)
