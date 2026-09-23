"""Import parsers. Raw text is stored verbatim by the store; parsers only
validate and produce structured views. Format errors are explicit
(IngestError with line numbers); unconvertible units are NOT errors - they
stay in place and surface as diagnostics in the derived layer."""

import json

from .units import make_quantity


class IngestError(Exception):
    def __init__(self, message, line=None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


def _float(token, what, line):
    try:
        return float(token)
    except ValueError:
        raise IngestError(f"{what} is not numeric: {token!r}", line)


def parse_peaks_1d(content: str) -> dict:
    header, peaks, colseen = {}, [], False
    for n, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            kv = line[1:].split(":", 1)
            if len(kv) == 2:
                header[kv[0].strip()] = kv[1].strip()
            continue
        parts = line.split()
        if parts[0] == "peak" and not colseen:
            colseen = True
            continue
        if len(parts) != 6:
            raise IngestError(f"expected 6 columns (peak shift unit intensity "
                              f"multiplicity integration), got {len(parts)}", n)
        pid, shift, unit, inten, mult, integ = parts
        peaks.append({
            "peak_id": pid,
            "shift": make_quantity(shift, unit, basis=header.get("reference")),
            "intensity": _float(inten, "intensity", n),
            "multiplicity": mult,
            "integration": _float(integ, "integration", n),
        })
    if "nucleus" not in header:
        raise IngestError("missing '# nucleus:' header")
    if header["nucleus"] not in ("1H", "13C"):
        raise IngestError(f"unsupported nucleus: {header['nucleus']!r}")
    if not peaks:
        raise IngestError("no peak rows found")
    ids = [p["peak_id"] for p in peaks]
    if len(set(ids)) != len(ids):
        raise IngestError("duplicate peak ids")
    return {"header": header, "peaks": peaks}


CORR_KINDS = ("COSY", "HSQC", "HMBC")


def parse_correlations(content: str) -> list:
    edges, colseen = [], False
    for n, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "edge" and not colseen:
            colseen = True
            continue
        if len(parts) != 4:
            raise IngestError(f"expected 4 columns (edge kind from to), "
                              f"got {len(parts)}", n)
        eid, kind, a, b = parts
        if kind not in CORR_KINDS:
            raise IngestError(f"unknown correlation kind: {kind!r}", n)
        edges.append({"edge_id": eid, "kind": kind, "from": a, "to": b})
    if not edges:
        raise IngestError("no correlation edges found")
    ids = [e["edge_id"] for e in edges]
    if len(set(ids)) != len(ids):
        raise IngestError("duplicate edge ids")
    return edges


def parse_molecule(content: str) -> dict:
    try:
        mol = json.loads(content)
    except json.JSONDecodeError as exc:
        raise IngestError(f"invalid JSON: {exc}")
    atoms = mol.get("atoms")
    bonds = mol.get("bonds")
    if not isinstance(atoms, list) or not atoms:
        raise IngestError("molecule needs a non-empty 'atoms' list")
    if not isinstance(bonds, list):
        raise IngestError("molecule needs a 'bonds' list")
    ids = [a.get("id") for a in atoms]
    if len(set(ids)) != len(ids) or any(i is None for i in ids):
        raise IngestError("atom ids must be unique and present")
    for a in atoms:
        for key in ("element", "env", "x", "y"):
            if key not in a:
                raise IngestError(f"atom {a.get('id')} missing '{key}'")
    idset = set(ids)
    for b in bonds:
        if len(b) != 2 or b[0] not in idset or b[1] not in idset:
            raise IngestError(f"bond references unknown atoms: {b}")
    mol.setdefault("coordinate_basis", "editor-2d")
    return mol


def parse_instrument(content: str) -> dict:
    try:
        inst = json.loads(content)
    except json.JSONDecodeError as exc:
        raise IngestError(f"invalid JSON: {exc}")
    for key in ("solvent", "frequency_mhz", "reference", "temperature_k"):
        if key not in inst:
            raise IngestError(f"instrument doc missing '{key}'")
    return inst


DEFAULT_RULES = {
    "shift_tol_ppm": {"1H": 0.3, "13C": 2.0},
    "overlap_tol_ppm": 0.03,
    "tie_eps": 0.02,
    "weights": {"cosy": 0.15, "hsqc": 0.3, "hmbc": 0.1},
    "seed": 20260919,
}


def parse_ruleset(content: str) -> dict:
    try:
        rules = json.loads(content)
    except json.JSONDecodeError as exc:
        raise IngestError(f"invalid JSON: {exc}")
    merged = json.loads(json.dumps(DEFAULT_RULES))
    for k, v in rules.items():
        if k == "format":
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


PARSERS = {
    "peaks_1d": parse_peaks_1d,
    "correlations_2d": parse_correlations,
    "molecule": parse_molecule,
    "instrument": parse_instrument,
    "ruleset": parse_ruleset,
}

FORMATS = {
    "peaks_1d": "nmr-peaks-1d/1",
    "correlations_2d": "nmr-corr-2d/1",
    "molecule": "mol-skeleton/1",
    "instrument": "instrument/1",
    "ruleset": "ruleset/1",
}
