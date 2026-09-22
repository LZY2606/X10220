"""Peak-list parser.

Raw text is preserved verbatim; every parsed row keeps the original string
representations. Normalization to floats happens here but the raw value is
never discarded; unconvertible values stay in place with a diagnostic.
"""

EDGE_TYPES = ("COSY", "HSQC", "HMBC")
META_KEYS = ("solvent", "frequency_mhz", "temperature_k", "reference")


def _to_float(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_peaklist(raw_text):
    meta = {}
    peaks = []
    edges = []
    diagnostics = []
    for lineno, line in enumerate(raw_text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        head = tokens[0].lower()
        if head == "peak":
            if len(tokens) < 4:
                diagnostics.append({"line": lineno, "raw": line,
                                    "error": "peak-needs: peak <nucleus> <label> <shift> [k=v ...]"})
                continue
            nucleus, label, shift_raw = tokens[1], tokens[2], tokens[3]
            kv = {}
            for tok in tokens[4:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k.strip()] = v.strip()
                else:
                    diagnostics.append({"line": lineno, "raw": line,
                                        "error": "dangling-token: %s" % tok})
            shift = _to_float(shift_raw)
            if shift is None:
                diagnostics.append({"line": lineno, "raw": line,
                                    "error": "unconvertible-shift: %r kept raw" % shift_raw})
            intensity_raw = kv.get("intensity")
            integral_raw = kv.get("integral")
            intensity = _to_float(intensity_raw) if intensity_raw is not None else None
            integral = _to_float(integral_raw) if integral_raw is not None else None
            if intensity_raw is not None and intensity is None:
                diagnostics.append({"line": lineno, "raw": line,
                                    "error": "unconvertible-intensity: %r kept raw" % intensity_raw})
            if integral_raw is not None and integral is None:
                diagnostics.append({"line": lineno, "raw": line,
                                    "error": "unconvertible-integral: %r kept raw" % integral_raw})
            peaks.append({
                "nucleus": nucleus,
                "label": label,
                "shift_raw": shift_raw,
                "shift_ppm": shift,
                "intensity_raw": intensity_raw,
                "intensity": intensity,
                "integral_raw": integral_raw,
                "integral": integral,
                "multiplicity": kv.get("mult"),
                "line": lineno,
                "raw": line,
            })
        elif head == "edge":
            if len(tokens) != 4:
                diagnostics.append({"line": lineno, "raw": line,
                                    "error": "edge-needs: edge <COSY|HSQC|HMBC> <a> <b>"})
                continue
            etype = tokens[1].upper()
            if etype not in EDGE_TYPES:
                diagnostics.append({"line": lineno, "raw": line,
                                    "error": "unknown-edge-type: %s" % tokens[1]})
                continue
            edges.append({"type": etype, "a": tokens[2], "b": tokens[3],
                          "line": lineno, "raw": line})
        elif ":" in stripped:
            key, value = stripped.split(":", 1)
            meta[key.strip()] = value.strip()
        else:
            diagnostics.append({"line": lineno, "raw": line,
                                "error": "unrecognized-line"})
    return {"meta": meta, "peaks": peaks, "edges": edges, "diagnostics": diagnostics}


def diff_inputs(old, new):
    """Field-level diff between two parsed inputs; drives local invalidation."""
    meta_changed = sorted(k for k in set(old["meta"]) | set(new["meta"])
                          if old["meta"].get(k) != new["meta"].get(k))
    old_peaks = {p["label"]: p for p in old["peaks"]}
    new_peaks = {p["label"]: p for p in new["peaks"]}
    peaks_changed = sorted(l for l in set(old_peaks) | set(new_peaks)
                           if old_peaks.get(l) != new_peaks.get(l))
    old_edges = sorted((e["type"], e["a"], e["b"]) for e in old["edges"])
    new_edges = sorted((e["type"], e["a"], e["b"]) for e in new["edges"])
    return {"meta": meta_changed, "peaks": peaks_changed,
            "edges": old_edges != new_edges}
