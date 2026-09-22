"""把已提交的原始记录规范化为领域对象（派生层第一步）。"""
from __future__ import annotations

from typing import Any, Dict, List

from .models import Conditions, Edge2D, ExpectedEdge, Peak1D, Site


def _record_rows(records: List[Any]) -> List[Dict[str, Any]]:
    rows = []
    for r in records:
        fields = r["fields"] if isinstance(r, dict) else r.fields
        rid = r["id"] if isinstance(r, dict) else r.id
        doc_id = r["doc_id"] if isinstance(r, dict) else r.doc_id
        doc_version = r["doc_version"] if isinstance(r, dict) else r.doc_version
        label = r["label"] if isinstance(r, dict) else r.label
        lineno = r["lineno"] if isinstance(r, dict) else r.lineno
        raw = r["raw"] if isinstance(r, dict) else r.raw
        kind = r["kind"] if isinstance(r, dict) else r.kind
        rows.append(dict(id=rid, doc_id=doc_id, doc_version=doc_version, label=label,
                         lineno=lineno, raw=raw, kind=kind, fields=fields))
    return rows


def build_conditions(headers: Dict[str, str]) -> Conditions:
    temp_repr = headers.get("temperature-k", "")
    spec_repr = headers.get("spectrometer-mhz", "")
    try:
        temp = float(temp_repr) if temp_repr else None
    except ValueError:
        temp = None
    try:
        spec = float(spec_repr) if spec_repr else None
    except ValueError:
        spec = None
    return Conditions(
        nucleus=headers.get("nucleus", ""),
        solvent=headers.get("solvent", ""),
        temperature_k=temp,
        temperature_repr=temp_repr,
        spectrometer_mhz=spec,
        spectrometer_repr=spec_repr,
        reference=headers.get("reference", ""),
    )


def build_peaks(rows: List[Dict[str, Any]], headers: Dict[str, str]) -> List[Peak1D]:
    cond = build_conditions(headers)
    peaks: List[Peak1D] = []
    for r in rows:
        f = r["fields"]
        peaks.append(Peak1D(
            id=r["id"], label=r["label"], doc_id=r["doc_id"], doc_version=r["doc_version"],
            lineno=r["lineno"], raw=r["raw"], nucleus=headers.get("nucleus", ""),
            shift_ppm=f.get("shift_ppm"), shift_repr=f.get("shift_repr", ""),
            shift_unit=f.get("shift_unit", "ppm"),
            intensity=f.get("intensity"), intensity_repr=f.get("intensity_repr", ""),
            intensity_unit=f.get("intensity_unit", "counts"),
            integral=f.get("integral"), integral_repr=f.get("integral_repr", ""),
            integral_unit=f.get("integral_unit", "H"),
            multiplicity=f.get("multiplicity", ""), conditions=cond,
        ))
    return peaks


def build_edges(rows: List[Dict[str, Any]], headers: Dict[str, str]) -> List[Edge2D]:
    nuclei = [n.strip() for n in headers.get("nuclei", "").split(",") if n.strip()]
    experiment = headers.get("experiment", "").upper()
    edges: List[Edge2D] = []
    for r in rows:
        f = r["fields"]
        edges.append(Edge2D(
            id=r["id"], label=r["label"], doc_id=r["doc_id"], doc_version=r["doc_version"],
            lineno=r["lineno"], raw=r["raw"], experiment=experiment, nuclei=nuclei,
            f1_ppm=f.get("f1_ppm"), f2_ppm=f.get("f2_ppm"),
            f1_repr=f.get("f1_repr", ""), f2_repr=f.get("f2_repr", ""),
            f1_unit=f.get("f1_unit", "ppm"), f2_unit=f.get("f2_unit", "ppm"),
            strength=f.get("strength", ""), strength_repr=f.get("strength_repr", ""),
        ))
    return edges


def build_sites(rows: List[Dict[str, Any]]) -> tuple[List[Site], List[ExpectedEdge]]:
    sites: List[Site] = []
    expected: List[ExpectedEdge] = []
    for r in rows:
        f = r["fields"]
        if r["kind"] == "site":
            sites.append(Site(
                id=r["id"], label=r["label"], doc_id=r["doc_id"], doc_version=r["doc_version"],
                carbon=f["carbon"], hydrogen=f["hydrogen"], element=f["element"],
                expected_shift_ppm=f.get("shift_ppm"),
                expected_shift_repr=f.get("shift_repr", ""),
                expected_shift_unit=f.get("shift_unit", "ppm"),
                symmetry_group=f.get("group", "") or f["hydrogen"],
                raw=r["raw"],
            ))
        else:
            expected.append(ExpectedEdge(id=r["id"], kind=r["kind"],
                                         a=f["from"], b=f["to"], edge_type=f.get("kind", "")))
    return sites, expected


def rows_from_docs(documents: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """documents: {logical_name, version, format, headers, records:[{id,...fields}]}"""
    pk1, pk2, sk = [], [], []
    for doc in documents:
        rows = _record_rows(doc["records"])
        fmt = doc["format"]
        if fmt == "NMRB-PEAKS1/1":
            pk1.append((doc, rows))
        elif fmt == "NMRB-PEAKS2/1":
            pk2.append((doc, rows))
        elif fmt == "NMRB-SKELETON/1":
            sk.append((doc, rows))
    return {"peak_docs": pk1, "edge_docs": pk2, "skel_docs": sk}
