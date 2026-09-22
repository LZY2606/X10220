"""Domain-specific parsers for the offline NMR case material.

Four small, copy-pasteable text formats are understood:

* ``.nbm``      - molecular skeleton: sites, symmetry groups, expected edges
    * ``.nbp``      - 1-D peak list (shift / intensity / integral / m / tags)
* ``.nb2``      - 2-D correlation edge list (COSY / HSQC / HMBC)
* ``.nmrcond``  - acquisition conditions (solvent, temperature, frequency, ref)

Design rules enforced here:

* The original bytes/text and every *original representation* (raw token)
  is preserved; normalization happens only in the derived layer.
* Unconvertible values stay in place and become diagnostics; the row is not
  dropped for drawing convenience.
* Fatal format errors (wrong kind, bad columns, non-numeric coordinate) raise
  :class:`DSLError` and the whole document is rejected by the import layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class DSLError(ValueError):
    """Fatal, document-level format error."""


@dataclass
class Diagnostic:
    level: str            # "warning" | "info"
    code: str
    message: str
    line: Optional[int] = None
    ref: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "ref": self.ref,
        }


@dataclass
class Parsed:
    kind: str
    header: Dict[str, str]
    rows: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "header": self.header,
            "rows": self.rows,
            "diagnostics": [d.as_dict() for d in self.diagnostics],
        }


def _strip(line: str) -> tuple[str, Optional[str]]:
    comment: Optional[str] = None
    if "#" in line:
        line, comment = line.split("#", 1)
        comment = comment.strip()
    return line.strip(), comment


def _parse_header(kind: str, lines: List[str]) -> Parsed:
    header: Dict[str, str] = {}
    body: List[tuple[int, str]] = []
    for lineno, raw in enumerate(lines, start=1):
        text, _comment = _strip(raw)
        if not text:
            continue
        if text.startswith("@"):
            key, _, value = text[1:].partition(":")
            header[key.strip().lower()] = value.strip()
            continue
        body.append((lineno, text))
    seen = "kind"
    expected = {"peaks", "edges", "mol", "conditions"}
    if header.get("kind", kind) not in expected:
        raise DSLError("unknown @kind: %r" % header.get("kind"))
    return Parsed(kind=header.get("kind", kind), header=header), body


def _as_float(token: str) -> float:
    try:
        return float(token)
    except ValueError as exc:
        raise DSLError("non-numeric coordinate %r" % token) from exc


def _split_rows(body, ncols, kind_label, alt_ncols=None):
    for lineno, text in body:
        parts = [p.strip() for p in text.split("|")]
        ok = len(parts) == ncols or (alt_ncols and len(parts) in alt_ncols)
        if not ok:
            raise DSLError(
                "%s row on line %d has %d columns, expected %d"
                % (kind_label, lineno, len(parts), ncols)
            )
        yield lineno, parts


def parse_peaks(text: str) -> Dict[str, Any]:
    """Parse a ``.nbp`` peak list.

    Columns: ``id | nucleus | shift_ppm | intensity | integral | multiplicity
    | tags`` (the trailing tags column is optional and comma separated).
    """
    (parsed, body) = _parse_header("peaks", text.splitlines())
    if parsed.kind != "peaks":
        raise DSLError("peak document must declare @kind: peaks")
    for lineno, parts in _split_rows(body, 7, "peak", alt_ncols=(6,)):
        if len(parts) == 6:
            parts.append("")
        pid, nucleus, raw_shift, raw_intensity, raw_integral, raw_mult, tags = parts
        if not pid:
            raise DSLError("peak on line %d has empty id" % lineno)
        nucleus = nucleus.upper()
        if nucleus not in ("1H", "13C"):
            raise DSLError(
                "peak %s: unsupported nucleus %r (expected 1H or 13C)"
                % (pid, nucleus)
            )
        shift = _as_float(raw_shift)
        row: Dict[str, Any] = {
            "local_id": pid,
            "nucleus": nucleus,
            "shift_ppm": shift,
            "shift_raw": raw_shift,
            "shift_unit": "ppm",
            "intensity_raw": raw_intensity,
            "integral_raw": raw_integral,
            "multiplicity": raw_mult,
            "line": lineno,
        }
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        row["tags"] = tag_list
        row["tags_raw"] = tags
        if raw_intensity.lower() in ("", "ovl", "overload", "sat"):
            parsed.diagnostics.append(
                Diagnostic(
                    "warning",
                    "unconvertible_intensity",
                    "peak %s: intensity %r kept as text; no numeric intensity"
                    % (pid, raw_intensity),
                    line=lineno,
                    ref=pid,
                )
            )
            row["intensity"] = None
        else:
            try:
                row["intensity"] = float(raw_intensity)
                row["intensity_unit"] = "arbitrary"
            except ValueError:
                raise DSLError(
                    "peak %s: non-numeric intensity %r (use OVL for overload)"
                    % (pid, raw_intensity)
                )
        try:
            row["integral"] = float(raw_integral)
            row["integral_unit"] = "H" if nucleus == "1H" else "C"
        except ValueError:
            row["integral"] = None
            parsed.diagnostics.append(
                Diagnostic(
                    "warning",
                    "unconvertible_integral",
                    "peak %s: integral %r kept as text" % (pid, raw_integral),
                    line=lineno,
                    ref=pid,
                )
            )
        parsed.rows.append(row)
    local_ids = [r["local_id"] for r in parsed.rows]
    if len(local_ids) != len(set(local_ids)):
        raise DSLError("duplicate peak id in peak list")
    return parsed.as_dict()


def parse_edges(text: str) -> Dict[str, Any]:
    """Parse a ``.nb2`` correlation edge list.

    Columns: ``id | kind | from_peak | to_peak | strength``
    Coordinates are attached during import by resolving peak references.
    """
    (parsed, body) = _parse_header("edges", text.splitlines())
    if parsed.kind != "edges":
        raise DSLError("edge document must declare @kind: edges")
    allowed = {"COSY", "HSQC", "HMBC"}
    for lineno, parts in _split_rows(body, 5, "edge"):
        eid, kind, from_peak, to_peak, strength_raw = parts
        kind = kind.upper()
        if kind not in allowed:
            raise DSLError(
                "edge %s: unknown kind %r (COSY/HSQC/HMBC)" % (eid, kind)
            )
        try:
            strength = float(strength_raw)
        except ValueError:
            raise DSLError(
                "edge %s: non-numeric strength %r" % (eid, strength_raw)
            )
        parsed.rows.append(
            {
                "local_id": eid,
                "edge_kind": kind,
                "from_peak_ref": from_peak,
                "to_peak_ref": to_peak,
                "strength": strength,
                "strength_raw": strength_raw,
                "strength_unit": "arbitrary",
                "line": lineno,
            }
        )
    local_ids = [r["local_id"] for r in parsed.rows]
    if len(local_ids) != len(set(local_ids)):
        raise DSLError("duplicate edge id")
    return parsed.as_dict()


def parse_mol(text: str) -> Dict[str, Any]:
    """Parse a ``.nbm`` molecular skeleton document."""
    (parsed, body) = _parse_header("mol", text.splitlines())
    if parsed.kind != "mol":
        raise DSLError("mol document must declare @kind: mol")
    sites: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    expected_edges: List[Dict[str, Any]] = []
    for lineno, text2 in body:
        tag, _, rest = text2.partition(":")
        tag = tag.strip().lower()
        fields = [p.strip() for p in rest.split("|")]
        if tag == "site":
            # id | nucleus | element | predicted_ppm | label | bound_carbon(optional)
            if len(fields) not in (5, 6):
                raise DSLError(
                    "site line %d expects 5 columns, got %d"
                    % (lineno, len(fields))
                )
            if len(fields) == 5:
                fields.append("")
            sid, nucleus, element, predicted_raw, label, bound_carbon = fields
            nucleus = nucleus.upper()
            if nucleus not in ("1H", "13C"):
                raise DSLError("site %s: unsupported nucleus" % sid)
            parsed.rows.append(
                {
                    "record": "site",
                    "local_id": sid,
                    "nucleus": nucleus,
                    "element": element,
                    "predicted_ppm": _as_float(predicted_raw),
                    "predicted_raw": predicted_raw,
                    "shift_unit": "ppm",
                    "label": label,
                    "bound_carbon": bound_carbon or None,
                    "line": lineno,
                }
            )
        elif tag == "group":
            # name | site ids (comma list)
            if len(fields) != 2:
                raise DSLError("group line %d expects 2 columns" % lineno)
            gname, members_raw = fields
            members = [m.strip() for m in members_raw.split(",") if m.strip()]
            if not members:
                raise DSLError("group %s has no members" % gname)
            parsed.rows.append(
                {
                    "record": "group",
                    "local_id": gname,
                    "member_site_ids": members,
                    "line": lineno,
                }
            )
        elif tag == "edge":
            # site_a | site_b | kind | note
            if len(fields) != 4:
                raise DSLError("edge line %d expects 4 columns" % lineno)
            a, b, ekind, note = fields
            parsed.rows.append(
                {
                    "record": "expected_edge",
                    "local_id": "%s--%s" % (a, b),
                    "site_a": a,
                    "site_b": b,
                    "edge_kind": ekind.upper(),
                    "note": note,
                    "line": lineno,
                }
            )
        else:
            raise DSLError("line %d: unknown record %r" % (lineno, tag))
    out = parsed.as_dict()
    out["sites"] = [r for r in out["rows"] if r["record"] == "site"]
    out["groups"] = [r for r in out["rows"] if r["record"] == "group"]
    out["expected_edges"] = [
        r for r in out["rows"] if r["record"] == "expected_edge"
    ]
    del out["rows"]
    return out


def parse_conditions(text: str) -> Dict[str, Any]:
    """Parse a ``.nmrcond`` acquisition-conditions document."""
    (parsed, body) = _parse_header("conditions", text.splitlines())
    if parsed.kind != "conditions":
        raise DSLError("conditions document must declare @kind: conditions")
    fields: Dict[str, Any] = {}
    for lineno, text2 in body:
        key, _, value = text2.partition(":")
        fields[key.strip().lower()] = (value.strip(), lineno)
    required = ("solvent", "temperature_k", "spectrometer_mhz", "reference")
    for req in required:
        if req not in fields:
            raise DSLError("conditions missing required field %r" % req)
    temp_raw, tline = fields["temperature_k"]
    freq_raw, fline = fields["spectrometer_mhz"]
    for name, raw, line in (
        ("temperature_k", temp_raw, tline),
        ("spectrometer_mhz", freq_raw, fline),
    ):
        try:
            fields[name] = float(raw)
        except ValueError:
            raise DSLError(
                "field %s on line %d is not numeric: %r" % (name, line, raw)
            )
    out = parsed.as_dict()
    ref_raw = fields["reference"][0]
    # reference looks like "TMS", "TMS+0.10ppm" or "DSS-0.05ppm"
    reference_std = ref_raw
    ref_offset_ppm = 0.0
    for sign in ("+", "-"):
        if sign in ref_raw:
            head, _, tail = ref_raw.partition(sign)
            number = tail.replace("ppm", "").strip()
            try:
                reference_std = head.strip()
                ref_offset_ppm = float(("%s%s") % (sign, number))
            except ValueError:
                raise DSLError("conditions: bad reference offset %r" % ref_raw)
            break
    out["fields"] = {
        "solvent": fields["solvent"][0],
        "solvent_raw": fields["solvent"][0],
        "temperature_k": fields["temperature_k"],
        "temperature_raw": temp_raw,
        "temperature_unit": "K",
        "spectrometer_mhz": fields["spectrometer_mhz"],
        "spectrometer_raw": freq_raw,
        "frequency_unit": "MHz",
        "reference": ref_raw,
        "reference_raw": ref_raw,
        "reference_standard": reference_std,
        "reference_offset_ppm": ref_offset_ppm,
        "reference_offset_unit": "ppm",
    }
    return out


PARSERS = {
    "peaks": parse_peaks,
    "edges": parse_edges,
    "mol": parse_mol,
    "conditions": parse_conditions,
}


def parse_document(kind: str, text: str) -> Dict[str, Any]:
    return PARSERS[kind](text)
