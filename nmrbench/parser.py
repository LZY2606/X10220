"""Import layer: raw text is preserved verbatim; normalization happens here.

Every record keeps its raw representation. Values that cannot be converted
stay in place with a diagnostic instead of being dropped.
"""
from .ids import doc_id

DOC_KINDS = ("structure", "context", "peaks_1h", "peaks_13c", "edges")

KIND_BY_MAGIC = {
    "# NMRSTRUCT v1": "structure",
    "# NMRCTX v1": "context",
    "# NMREDGES v1": "edges",
}

KIND_BY_FILENAME = {
    ".nmrstruct": "structure",
    ".ctx": "context",
    ".edges": "edges",
}


def detect_kind(filename: str, text: str) -> str:
    first = text.splitlines()[0].strip() if text.strip() else ""
    if first in KIND_BY_MAGIC:
        return KIND_BY_MAGIC[first]
    if first == "# NMR1D v1":
        for line in text.splitlines():
            if line.startswith("# nucleus="):
                nuc = line.split("=", 1)[1].strip()
                return "peaks_1h" if nuc == "1H" else "peaks_13c"
        return "peaks_1h"
    lower = filename.lower()
    for suffix, kind in KIND_BY_FILENAME.items():
        if lower.endswith(suffix):
            return kind
    return "peaks_1h"


def _diag(level, code, message, raw=None):
    d = {"level": level, "code": code, "message": message}
    if raw is not None:
        d["raw"] = raw
    return d


def _headers(text):
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") and "=" in s:
            key, val = s[1:].split("=", 1)
            out[key.strip()] = val.strip()
    return out


def _data_lines(text):
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            yield s


def parse_1d(text, nucleus):
    diags, records = [], []
    headers = _headers(text)
    for req in ("solvent", "temperature_K", "frequency_MHz", "reference"):
        if req not in headers:
            diags.append(_diag("error", "MISSING_HEADER",
                               f"required header '{req}' missing"))
    for idx, line in enumerate(_data_lines(text), start=1):
        parts = line.split()
        if len(parts) != 4:
            diags.append(_diag("error", "BAD_COLUMN_COUNT",
                               f"line {idx}: expected 4 columns, got {len(parts)}",
                               raw=line))
            continue
        raw_shift, raw_int, raw_integral, mult = parts
        rec = {
            "line": idx,
            "nucleus": nucleus,
            "shift_raw": raw_shift,
            "intensity_raw": raw_int,
            "integral_raw": raw_integral,
            "multiplicity_raw": mult,
            "shift_ppm": None,
            "intensity": None,
            "integral": None,
            "multiplicity": mult.split("|")[0],
            "flags": [f for f in mult.split("|")[1:] if f],
            "diagnostics": [],
        }
        try:
            rec["shift_ppm"] = float(raw_shift)
        except ValueError:
            rec["diagnostics"].append(_diag(
                "error", "UNCONVERTIBLE_SHIFT",
                f"line {idx}: shift '{raw_shift}' is not a number; raw value kept in place",
                raw=raw_shift))
            diags.append(_diag("error", "UNCONVERTIBLE_SHIFT",
                               f"line {idx}: shift '{raw_shift}' kept raw, excluded from matching"))
        for field, raw in (("intensity", raw_int), ("integral", raw_integral)):
            try:
                rec[field] = float(raw)
            except ValueError:
                rec["diagnostics"].append(_diag(
                    "error", "UNCONVERTIBLE_VALUE",
                    f"line {idx}: {field} '{raw}' is not a number; raw kept", raw=raw))
        records.append(rec)
    return {"nucleus": nucleus, "headers": headers, "records": records,
            "diagnostics": diags}


def parse_edges(text):
    diags, records = [], []
    headers = _headers(text)
    for idx, line in enumerate(_data_lines(text), start=1):
        parts = line.split()
        if len(parts) != 4:
            diags.append(_diag("error", "BAD_COLUMN_COUNT",
                               f"line {idx}: expected 4 columns, got {len(parts)}",
                               raw=line))
            continue
        exp, raw_a, raw_b, inten = parts
        if exp not in ("cosy", "hsqc", "hmbc"):
            diags.append(_diag("error", "UNKNOWN_EXPERIMENT",
                               f"line {idx}: unknown experiment '{exp}'", raw=line))
            continue
        rec = {"line": idx, "experiment": exp, "shift_a_raw": raw_a,
               "shift_b_raw": raw_b, "intensity_raw": inten,
               "shift_a": None, "shift_b": None, "diagnostics": []}
        for field, raw in (("shift_a", raw_a), ("shift_b", raw_b)):
            try:
                rec[field] = float(raw)
            except ValueError:
                rec["diagnostics"].append(_diag(
                    "error", "UNCONVERTIBLE_SHIFT",
                    f"line {idx}: {field} '{raw}' not a number; raw kept", raw=raw))
        records.append(rec)
    return {"headers": headers, "records": records, "diagnostics": diags}


def parse_structure(text):
    diags = []
    sites, bonds, equiv = [], [], []
    for idx, line in enumerate(_data_lines(text), start=1):
        parts = line.split()
        tag = parts[0]
        if tag == "site":
            if len(parts) != 6:
                diags.append(_diag("error", "BAD_SITE_LINE",
                                   f"line {idx}: expected 6 fields", raw=line))
                continue
            sid, element, label, ref_h, ref_c, hcount = parts
            site = {"site_id": sid, "element": element, "label": label,
                    "ref_1h_raw": ref_h, "ref_13c_raw": ref_c,
                    "ref_1h_ppm": None, "ref_13c_ppm": None,
                    "h_count": None, "diagnostics": []}
            for field, raw in (("ref_1h_ppm", ref_h), ("ref_13c_ppm", ref_c)):
                if raw == "-":
                    continue
                try:
                    site[field] = float(raw)
                except ValueError:
                    site["diagnostics"].append(_diag(
                        "error", "UNCONVERTIBLE_REF",
                        f"site {sid}: {field} '{raw}' not a number; raw kept", raw=raw))
            try:
                site["h_count"] = int(hcount)
            except ValueError:
                site["diagnostics"].append(_diag(
                    "error", "UNCONVERTIBLE_HCOUNT",
                    f"site {sid}: h_count '{hcount}' not an integer; raw kept", raw=hcount))
            sites.append(site)
        elif tag == "bond":
            if len(parts) != 3:
                diags.append(_diag("error", "BAD_BOND_LINE", f"line {idx}", raw=line))
                continue
            bonds.append({"a": parts[1], "b": parts[2]})
        elif tag == "equiv":
            if len(parts) < 3:
                diags.append(_diag("error", "BAD_EQUIV_LINE", f"line {idx}", raw=line))
                continue
            equiv.append({"group": parts[1], "sites": parts[2:]})
        else:
            diags.append(_diag("error", "UNKNOWN_DIRECTIVE",
                               f"line {idx}: unknown directive '{tag}'", raw=line))
    return {"sites": sites, "bonds": bonds, "equiv": equiv, "diagnostics": diags}


def parse_context(text):
    diags = []
    values = {}
    for idx, line in enumerate(_data_lines(text), start=1):
        if "=" not in line:
            diags.append(_diag("error", "BAD_CONTEXT_LINE",
                               f"line {idx}: expected key=value", raw=line))
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip()
    for req in ("solvent", "temperature_K", "freq_1h_MHz", "freq_13c_MHz",
                "ref_1h", "ref_13c"):
        if req not in values:
            diags.append(_diag("error", "MISSING_CONTEXT_KEY",
                               f"required context key '{req}' missing"))
    return {"values": values, "diagnostics": diags}


PARSERS = {
    "peaks_1h": lambda t: parse_1d(t, "1H"),
    "peaks_13c": lambda t: parse_1d(t, "13C"),
    "edges": parse_edges,
    "structure": parse_structure,
    "context": parse_context,
}


def parse_document(kind: str, filename: str, raw_text: str) -> dict:
    parsed = PARSERS[kind](raw_text)
    return {
        "doc_id": doc_id(kind, raw_text),
        "kind": kind,
        "filename": filename,
        "raw_text": raw_text,
        "parsed": parsed,
        "diagnostics": parsed.get("diagnostics", []),
    }
