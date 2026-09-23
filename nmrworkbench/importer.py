"""Import layer for copy-pasteable peak-list text or structured JSON.

Line format (comments start with '#')::

    context,solvent,CDCl3
    context,instrument_mhz,400.13
    context,temperature_K,298
    context,reference,TMS
    peak,1H,pk-H2,2.30,0.81,2.0,q
    peak,13C,pk-C1,173.8,0.35,,
    edge,COSY,pk-H2,pk-H3
    edge,HSQC,pk-H2,pk-C2
    edge,HMBC,pk-H2,pk-C1

Raw representations are always preserved.  Values that cannot be converted
stay in place with a diagnostic instead of being dropped; structurally
malformed lines are hard format errors.
"""
import json

EDGE_TYPES = ("COSY", "HSQC", "HMBC")
NUCLEI = ("1H", "13C")
CONTEXT_KEYS = ("solvent", "instrument_mhz", "temperature_K", "reference")


class ImportError_(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors))


def _float_or_diag(raw, what, peak_key, diagnostics):
    try:
        return float(raw), None
    except (TypeError, ValueError):
        diagnostics.append({
            "object": peak_key,
            "field": what,
            "raw": raw,
            "message": "value %r for %s of %s is not numeric; kept raw, excluded from derived layers"
                       % (raw, what, peak_key),
        })
        return None, "unconvertible"


def parse_text(text):
    """Parse peak-list text -> (content, errors).  Never raises."""
    content = {"context": {}, "peaks": [], "edges": [], "raw_text": text}
    errors = []
    diagnostics = []
    seen_peaks = set()
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        tag = parts[0].lower()
        if tag == "context":
            if len(parts) != 3:
                errors.append("line %d: context needs 3 fields, got %d"
                              % (lineno, len(parts)))
                continue
            key, value = parts[1], parts[2]
            if key not in CONTEXT_KEYS:
                errors.append("line %d: unknown context key %r (expected one of %s)"
                              % (lineno, key, ", ".join(CONTEXT_KEYS)))
                continue
            content["context"][key] = value
        elif tag == "peak":
            if len(parts) != 7:
                errors.append("line %d: peak needs 7 fields "
                              "(peak,nucleus,key,shift,intensity,integration,multiplicity), got %d"
                              % (lineno, len(parts)))
                continue
            _, nucleus, key, shift_raw, inten_raw, integ_raw, mult = parts
            if nucleus not in NUCLEI:
                errors.append("line %d: unknown nucleus %r (expected 1H or 13C)"
                              % (lineno, nucleus))
                continue
            if not key:
                errors.append("line %d: peak key must not be empty" % lineno)
                continue
            if key in seen_peaks:
                errors.append("line %d: duplicate peak key %r" % (lineno, key))
                continue
            seen_peaks.add(key)
            shift, _ = _float_or_diag(shift_raw, "shift_ppm", key, diagnostics)
            intensity, _ = _float_or_diag(inten_raw, "intensity", key, diagnostics)
            integration = None
            if integ_raw:
                integration, _ = _float_or_diag(integ_raw, "integration", key, diagnostics)
            content["peaks"].append({
                "peak_key": key,
                "nucleus": nucleus,
                "shift_ppm": shift,
                "shift_raw": shift_raw,
                "intensity": intensity,
                "intensity_raw": inten_raw,
                "integration": integration,
                "integration_raw": integ_raw,
                "multiplicity": mult or None,
                "unit": "ppm",
                "line": lineno,
            })
        elif tag == "edge":
            if len(parts) != 4:
                errors.append("line %d: edge needs 4 fields (edge,TYPE,a,b), got %d"
                              % (lineno, len(parts)))
                continue
            _, etype, a_key, b_key = parts
            etype = etype.upper()
            if etype not in EDGE_TYPES:
                errors.append("line %d: unknown edge type %r (expected COSY/HSQC/HMBC)"
                              % (lineno, etype))
                continue
            content["edges"].append({
                "edge_type": etype,
                "a_key": a_key,
                "b_key": b_key,
                "line": lineno,
            })
        else:
            errors.append("line %d: unknown record tag %r (expected context/peak/edge)"
                          % (lineno, parts[0]))
    # cross-reference checks
    for edge in content["edges"]:
        for k in (edge["a_key"], edge["b_key"]):
            if k not in seen_peaks:
                errors.append("line %d: %s edge references unknown peak %r"
                              % (edge["line"], edge["edge_type"], k))
    content["diagnostics"] = diagnostics
    return content, errors


def parse_payload(body):
    """Accept either {'text': ...} or structured {'context':..,'peaks':..,'edges':..}."""
    if isinstance(body, dict) and "text" in body:
        return parse_text(body["text"])
    if isinstance(body, dict) and ("peaks" in body or "context" in body):
        text = body.get("raw_text") or json.dumps(body, ensure_ascii=False)
        content = {
            "context": dict(body.get("context") or {}),
            "peaks": [],
            "edges": [],
            "raw_text": text,
        }
        errors = []
        diagnostics = []
        for pk in body.get("peaks") or []:
            key = pk.get("peak_key") or ""
            nucleus = pk.get("nucleus") or ""
            if nucleus not in NUCLEI:
                errors.append("peak %r: unknown nucleus %r" % (key, nucleus))
                continue
            shift_raw = pk.get("shift_raw", pk.get("shift_ppm"))
            shift, _ = _float_or_diag(shift_raw, "shift_ppm", key, diagnostics)
            content["peaks"].append({
                "peak_key": key,
                "nucleus": nucleus,
                "shift_ppm": shift,
                "shift_raw": None if shift_raw is None else str(shift_raw),
                "intensity": pk.get("intensity"),
                "intensity_raw": str(pk.get("intensity")),
                "integration": pk.get("integration"),
                "integration_raw": str(pk.get("integration")),
                "multiplicity": pk.get("multiplicity"),
                "unit": pk.get("unit", "ppm"),
                "line": 0,
            })
        for e in body.get("edges") or []:
            etype = (e.get("edge_type") or "").upper()
            if etype not in EDGE_TYPES:
                errors.append("edge: unknown type %r" % etype)
                continue
            content["edges"].append({
                "edge_type": etype,
                "a_key": e.get("a_key"),
                "b_key": e.get("b_key"),
                "line": 0,
            })
        content["diagnostics"] = diagnostics
        return content, errors
    return None, ["payload must contain 'text' or structured 'peaks'/'context'"]
