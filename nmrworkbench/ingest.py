"""Import layer: parse raw NMR documents and normalize *on the derived layer*.

The raw text/JSON bytes are stored verbatim by the service and are never
overwritten.  Parsing happens every time a projection is built, so a bad or
unconvertible value never blocks import: it stays at its original position
with a structured diagnostic instead of being silently dropped.
"""
from __future__ import annotations

import json
import re

DOC_IDS = {
    "peaks_1h", "peaks_13c",
    "edges_cosy", "edges_hsqc", "edges_hmbc",
    "conditions", "skeleton", "params",
}

PEAK_DOCS = {"peaks_1h", "peaks_13c"}
EDGE_DOCS = {"edges_cosy", "edges_hsqc", "edges_hmbc"}


class IngestError(Exception):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("; ".join(d["message"] for d in diagnostics))


def _diag(message, path="<doc>", raw=None, line=None):
    d = {"path": path, "message": message}
    if raw is not None:
        d["raw"] = raw
    if line is not None:
        d["line"] = line
    return d


# --------------------------------------------------------------------- parse
_SHIFT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(ppm|hz)?$", re.I)
_INTEG_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|c)?$", re.I)


def parse_document(text, declared_doc=None, fmt="auto"):
    """Parse raw content into a plain dict.  Raises IngestError on bad format."""
    if not isinstance(text, str) or not text.strip():
        raise IngestError([_diag("empty document")])
    stripped = text.lstrip()
    if fmt in ("auto", "json") and stripped[:1] in "{[":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            if fmt == "json" or stripped[:1] in "{[":
                raise IngestError([_diag("invalid JSON: %s" % exc.msg, line=exc.lineno)])
        else:
            if not isinstance(raw, dict):
                raise IngestError([_diag("top-level JSON value must be an object")])
            doc_id = raw.get("doc") or declared_doc
            _validate(doc_id, raw)
            return doc_id, raw
    if fmt == "json":
        raise IngestError([_diag("document declared as JSON but is not an object")])
    return _parse_text(text, declared_doc)


def _parse_text(text, declared_doc):
    """Paste-friendly peaks table::

       doc: peaks_1h
       id,shift,intensity,integration,multiplicity
       P1,0.92 ppm,0.81,6H,d
    """
    lines = [(i + 1, ln.strip()) for i, ln in enumerate(text.splitlines())]
    lines = [(i, ln) for i, ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise IngestError([_diag("document contains no non-empty lines")])
    doc_id = declared_doc
    header_idx = None
    for pos, (lineno, ln) in enumerate(lines):
        if ln.lower().startswith("doc:"):
            doc_id = ln.split(":", 1)[1].strip()
        if ln.lower().startswith("id,"):
            header_idx = pos
            header_line = (lineno, ln)
            break
    if header_idx is None:
        raise IngestError([_diag("missing header row starting with 'id,'")])
    if doc_id not in PEAK_DOCS:
        raise IngestError([_diag(
            "text format only supports peaks documents (got %r)" % doc_id)])
    columns = [c.strip() for c in header_line[1].split(",")]
    if "shift" not in columns:
        raise IngestError([_diag("table must have a 'shift' column", line=header_line[0])])
    peaks = []
    diagnostics = []
    for lineno, ln in lines[header_idx + 1:]:
        fields = [c.strip() for c in ln.split(",")]
        if len(fields) != len(columns):
            diagnostics.append(_diag(
                "row has %d fields, expected %d" % (len(fields), len(columns)),
                line=lineno, raw=ln))
            continue
        row = dict(zip(columns, fields))
        if not row.get("id"):
            diagnostics.append(_diag("peak id is required", line=lineno, raw=ln))
            continue
        if not row.get("shift"):
            diagnostics.append(_diag("peak shift is required", line=lineno, raw=ln))
            continue
        peaks.append(row)
    if not peaks:
        diagnostics.append(_diag("no parseable peak rows"))
        raise IngestError(diagnostics)
    raw = {"doc": doc_id, "nucleus": "1H" if doc_id == "peaks_1h" else "13C",
           "format": "text-table", "peaks": peaks}
    if diagnostics:
        # structural row errors are fatal and reported up front
        raise IngestError(diagnostics)
    return doc_id, raw


def _validate(doc_id, raw):
    if doc_id not in DOC_IDS:
        raise IngestError([_diag("unknown document id %r" % doc_id, raw=doc_id)])
    if doc_id in PEAK_DOCS:
        if not isinstance(raw.get("peaks"), list) or not raw["peaks"]:
            raise IngestError([_diag("peaks document needs a non-empty 'peaks' list")])
        for i, p in enumerate(raw["peaks"]):
            if not isinstance(p, dict) or not p.get("id") or not p.get("shift"):
                raise IngestError([_diag(
                    "each peak needs non-empty 'id' and 'shift'",
                    path="peaks[%d]" % i)])
    elif doc_id in EDGE_DOCS:
        edges = raw.get("edges")
        if not isinstance(edges, list):
            raise IngestError([_diag("edge document needs an 'edges' list")])
        for i, e in enumerate(edges):
            ends = e.get("endpoints") if isinstance(e, dict) else None
            if not isinstance(ends, list) or len(ends) != 2:
                raise IngestError([_diag(
                    "each edge needs exactly two endpoints", path="edges[%d]" % i)])
            for ep in ends:
                if not isinstance(ep, dict) or not ep.get("peak") or not ep.get("shift"):
                    raise IngestError([_diag(
                        "each endpoint needs 'peak' and 'shift'",
                        path="edges[%d].endpoints" % i)])
    elif doc_id == "conditions":
        for key in ("solvent", "reference", "frequency"):
            if not raw.get(key):
                raise IngestError([_diag("conditions.%s is required" % key)])
    elif doc_id == "skeleton":
        if not isinstance(raw.get("atoms"), list) or not raw["atoms"]:
            raise IngestError([_diag("skeleton needs a non-empty 'atoms' list")])


# ------------------------------------------------------------- normalization
def normalize_document(doc_id, raw, conditions=None):
    """Return (normalized_dict, diagnostics).  Never raises: every problem is a
    diagnostic attached to the offending item at its original position."""
    conditions = conditions or {}
    diagnostics = []
    if doc_id == "conditions":
        out, diag = _normalize_conditions(raw)
        return out, diag
    if doc_id in PEAK_DOCS:
        peaks = []
        for i, p in enumerate(raw.get("peaks", [])):
            q, d = _normalize_peak(p, conditions, "peaks[%d]" % i)
            peaks.append(q)
            diagnostics.extend(d)
        out = dict(raw)
        out["peaks"] = peaks
        return out, diagnostics
    if doc_id in EDGE_DOCS:
        edges = []
        for i, e in enumerate(raw.get("edges", [])):
            ends = []
            for j, ep in enumerate(e["endpoints"]):
                ne, d = _normalize_endpoint(ep, conditions, "edges[%d].endpoints[%d]" % (i, j))
                ends.append(ne)
                diagnostics.extend(d)
            ne = dict(e)
            ne["endpoints"] = ends
            edges.append(ne)
        out = dict(raw)
        out["edges"] = edges
        return out, diagnostics
    if doc_id == "skeleton":
        return _normalize_skeleton(raw, conditions)
    return dict(raw), diagnostics


def _normalize_peak(p, conditions, path):
    q = dict(p)
    diagnostics = []
    shift_ppm, diag = _shift_to_ppm(p.get("shift"), conditions, path + ".shift")
    q["shift_ppm"] = shift_ppm
    diagnostics.extend(diag)
    q["intensity_value"] = _number(p.get("intensity"), path + ".intensity", diagnostics)
    integ = p.get("integration")
    if integ is not None:
        m = _INTEG_RE.match(str(integ).strip())
        if m:
            q["integration_value"] = float(m.group(1))
            q["integration_unit"] = (m.group(2) or "H").upper()
        else:
            diagnostics.append(_diag(
                "unparseable integration %r; kept raw" % integ, path + ".integration", integ))
    return q, diagnostics


def _normalize_endpoint(ep, conditions, path):
    ne = dict(ep)
    diagnostics = []
    shift_ppm, diag = _shift_to_ppm(ep.get("shift"), conditions, path + ".shift")
    ne["shift_ppm"] = shift_ppm
    diagnostics.extend(diag)
    return ne, diagnostics


def _normalize_skeleton(raw, conditions):
    out = dict(raw)
    diagnostics = []
    atoms = []
    for i, a in enumerate(raw.get("atoms", [])):
        na = dict(a)
        if a.get("predicted_shift") is None:
            na["predicted_shift_ppm"] = None
        else:
            ppm, diag = _shift_to_ppm(
                a["predicted_shift"], conditions, "atoms[%d].predicted_shift" % i)
            na["predicted_shift_ppm"] = ppm
            diagnostics.extend(diag)
        atoms.append(na)
    out["atoms"] = atoms
    return out, diagnostics


def _shift_to_ppm(raw, conditions, path):
    if raw is None:
        return None, []
    text = str(raw).strip()
    m = _SHIFT_RE.match(text)
    if not m:
        return None, [_diag(
            "unparseable chemical shift %r; kept raw, no value derived" % text, path, text)]
    value = float(m.group(1))
    unit = (m.group(2) or "ppm").lower()
    if unit == "ppm":
        if m.group(2) is None:
            return value, [_diag("shift %r has no unit; assumed ppm (TMS reference)" % text, path, text)]
        return value, []
    # Hz -> ppm needs the instrument frequency, an explicit coordinate basis
    freq = conditions.get("frequency_mhz")
    if not freq:
        return None, [_diag(
            "shift %r given in Hz but instrument frequency is unavailable; "
            "left at original position unnormalized" % text, path, text)]
    return round(value / freq, 6), []


def _number(raw, path, diagnostics):
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        diagnostics.append(_diag("unparseable number %r; kept raw" % raw, path, raw))
        return None


def _normalize_conditions(raw):
    out = dict(raw)
    diagnostics = []
    freq = raw.get("frequency")
    if freq is not None:
        m = re.match(r"^([\d.]+)\s*MHz$", str(freq).strip(), re.I)
        if m:
            out["frequency_mhz"] = float(m.group(1))
        else:
            diagnostics.append(_diag(
                "frequency %r not understood as MHz; kept raw" % freq, "frequency", freq))
    temp = raw.get("temperature")
    if temp is not None:
        m = re.match(r"^([-\d.]+)\s*K$", str(temp).strip(), re.I)
        if m:
            out["temperature_k"] = float(m.group(1))
        else:
            diagnostics.append(_diag(
                "temperature %r not understood as K; kept raw" % temp, "temperature", temp))
    return out, diagnostics
