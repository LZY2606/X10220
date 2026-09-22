"""从原始文档版本集合物化领域实体：骨架、核、峰、相关边。

身份稳定：实体 ID 由源文档哈希 + 段内行序号 + 行内容决定，
跨导出/再导入保持不变；不使用数据库自增主键。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .ident import digest
from .parser import ParsedDocument, Row

DEFAULT_CONTEXT = {
    "solvent": None,
    "temperature_k": None,
    "reference_standard": None,
    "spectrometer_frequency_hz": {},
    "basis": {"shift_scale": "ppm_relative_TMS",
              "frequency_unit": "Hz", "temperature_unit": "K"},
}


def _r6(x: Optional[float]) -> Optional[float]:
    return round(float(x), 6) if isinstance(x, (int, float)) else None


@dataclass
class Atom:
    id: str
    label: str
    element: str
    equivalence: int
    bonds: List[str]
    kind: str            # 1H / 13C
    source_id: str
    lineno: int
    predicted_shift: Optional[float] = None
    predicted_shift_raw: Optional[str] = None
    multiplicity: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Peak:
    id: str
    ref: str
    kind: str            # 1H / 13C
    shift: Optional[float]
    shift_raw: str
    shift_unit: str
    intensity: Optional[float]
    intensity_raw: str
    integral: Optional[float]
    integral_raw: str
    integral_unit: str
    multiplicity: Optional[str]
    multiplicity_raw: Optional[str]
    source_id: str
    version: str
    lineno: int

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Edge:
    id: str
    kind: str            # COSY / HSQC / HMBC
    a_raw: str
    b_raw: str
    a_refs: List[str]
    b_refs: List[str]
    delta_raw: Optional[str]
    delta_shifts: List[Dict[str, Any]]
    source_id: str
    version: str
    lineno: int
    a_peak_ids: List[str] = field(default_factory=list)
    b_peak_ids: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SourceDocument:
    id: str
    slot: str
    version: str
    parent_id: Optional[str]
    raw_text: str
    format: str
    meta: Dict[str, str]
    diagnostics: List[Dict[str, Any]]
    submitted_at: str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Dataset:
    sources: List[SourceDocument]
    atoms: Dict[str, Atom]
    peaks: Dict[str, Peak]
    edges: List[Edge]
    context: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sources": [s.to_dict() for s in self.sources],
            "atoms": {k: a.to_dict() for k, a in self.atoms.items()},
            "peaks": {k: p.to_dict() for k, p in self.peaks.items()},
            "edges": [e.to_dict() for e in self.edges],
            "context": self.context,
            "warnings": list(self.warnings),
        }


def make_source_id(slot: str, version: str, raw_text: str) -> str:
    return "src_" + digest({"slot": slot, "version": version,
                            "raw_text": raw_text})


def _peak_id(source_id: str, kind: str, index: int, row: Row) -> str:
    payload = {"src": source_id, "sec": kind, "idx": index,
               "raw": row.raw_line}
    return "pk_" + digest(payload)[:14]


def _edge_id(source_id: str, row: Row) -> str:
    return "ed_" + digest({"src": source_id, "ln": row.lineno,
                           "raw": row.raw_line})[:14]


def _atom_id(source_id: str, label: str) -> str:
    return "at_" + digest({"src": source_id, "label": label})[:14]


def build_context(meta_docs: List[ParsedDocument]) -> Dict[str, Any]:
    ctx = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in DEFAULT_CONTEXT.items()}
    for doc in meta_docs:
        meta = doc.meta
        if "solvent" in meta:
            ctx["solvent"] = meta["solvent"]
        if "temperature_k" in meta:
            tv = _num_or_none(meta["temperature_k"])
            ctx["temperature_k"] = {"raw": meta["temperature_k"],
                                    "value": tv, "unit": "K"}
        if "reference_standard" in meta:
            ctx["reference_standard"] = meta["reference_standard"]
        freq = {}
        if "frequency_hz" in meta:
            freq["_raw"] = meta["frequency_hz"]
            freq["value_hz"] = _num_or_none(meta["frequency_hz"])
        if "proton_frequency_mhz" in meta:
            freq["proton_mhz"] = {
                "raw": meta["proton_frequency_mhz"],
                "value": _num_or_none(meta["proton_frequency_mhz"]),
                "unit": "MHz"}
        if "carbon_frequency_mhz" in meta:
            freq["carbon_mhz"] = {
                "raw": meta["carbon_frequency_mhz"],
                "value": _num_or_none(meta["carbon_frequency_mhz"]),
                "unit": "MHz"}
        if freq:
            ctx["spectrometer_frequency_hz"].update(freq)
    return ctx


def _num_or_none(raw: str) -> Optional[float]:
    try:
        return float(raw)
    except ValueError:
        return None


def build_dataset(source_records: List[Dict[str, Any]],
                  parsed_by_id: Dict[str, ParsedDocument]) -> Dataset:
    """source_records: 已按时间排序的提交事件（同 slot 后者为当前版本）。"""
    atoms: Dict[str, Atom] = {}
    peaks: Dict[str, Peak] = {}
    edges: List[Edge] = []
    docs_meta: List[ParsedDocument] = []
    built_sources: List[SourceDocument] = []

    latest_for_slot: Dict[str, Dict[str, Any]] = {}
    for rec in source_records:
        latest_for_slot[rec["slot"]] = rec

    for rec in source_records:
        doc = parsed_by_id[rec["source_id"]]
        built_sources.append(SourceDocument(
            id=rec["source_id"], slot=rec["slot"], version=rec["version"],
            parent_id=rec.get("parent_id"), raw_text=doc.raw_text,
            format=doc.format, meta=dict(doc.meta),
            diagnostics=[d.to_dict() for d in doc.diagnostics],
            submitted_at=rec["submitted_at"]))
        docs_meta.append(doc)

    for rec in latest_for_slot.values():
        doc = parsed_by_id[rec["source_id"]]
        sid = rec["source_id"]
        ver = rec["version"]
        for row in doc.rows.get("skeleton", []):
            d = row.data
            if not d:
                continue
            aid = _atom_id(sid, d["atom"])
            atoms[d["atom"]] = Atom(
                id=aid, label=d["atom"], element=d["element"],
                equivalence=d.get("equivalence") or 1,
                bonds=list(d.get("bonds_raw", [])),
                kind="13C" if d["element"] == "C" else "1H",
                source_id=sid, lineno=row.lineno)
        for row in doc.rows.get("predict", []):
            d = row.data
            if not d or d.get("nucleus") not in atoms:
                continue
            atom = atoms[d["nucleus"]]
            sh = d.get("shift") or {}
            atom.predicted_shift = _r6(sh.get("value"))
            atom.predicted_shift_raw = sh.get("raw")
            atom.multiplicity = d.get("multiplicity")
        for kind in ("1H", "13C"):
            for index, row in enumerate(doc.rows.get(kind, []), start=1):
                d = row.data
                if not d:
                    continue
                sh = d["shift"]
                pid = _peak_id(sid, kind, index, row)
                peaks[f"{kind}:{index}"] = Peak(
                    id=pid, ref=f"p{index}", kind=kind,
                    shift=_r6(sh.get("value")), shift_raw=sh.get("raw"),
                    shift_unit=sh.get("unit") or "ppm",
                    intensity=d.get("intensity"), intensity_raw=d.get("intensity_raw"),
                    integral=d.get("integral"), integral_raw=d.get("integral_raw"),
                    integral_unit=d.get("integral_unit"),
                    multiplicity=d.get("multiplicity"),
                    multiplicity_raw=d.get("multiplicity_raw"),
                    source_id=sid, version=ver, lineno=row.lineno)
        for kind in ("COSY", "HSQC", "HMBC"):
            for row in doc.rows.get(kind, []):
                d = row.data
                if not d:
                    continue
                edges.append(Edge(
                    id=_edge_id(sid, row), kind=kind,
                    a_raw=d["a_raw"], b_raw=d["b_raw"],
                    a_refs=list(d["a_refs"]), b_refs=list(d["b_refs"]),
                    delta_raw=d.get("delta"),
                    delta_shifts=list(d.get("delta_shifts", [])),
                    source_id=sid, version=ver, lineno=row.lineno))

    # 边引用解析到峰：按边种类与端点位置判定核种类。
    # COSY 两端都是 1H；HSQC/HMBC 第一端 1H、第二端 13C。
    # 显式 h1/c1 前缀优先；p1 按位置种类解释；重叠组 {..} 保持多解析。
    warnings: List[str] = []
    by_kind: Dict[str, Dict[str, Peak]] = {"1H": {}, "13C": {}}
    for key, pk in peaks.items():
        kind, _, idx = key.partition(":")
        by_kind[kind][f"p{idx}"] = pk
        by_kind[kind][("h" if kind == "1H" else "c") + idx] = pk

    def _resolve(refs: List[str], kind: str, edge_kind: str,
                 endpoint: str) -> Tuple[List[str], List[str]]:
        if edge_kind == "COSY":
            want = "1H"
        else:
            want = "1H" if endpoint == "a" else "13C"
        resolved, missed = [], []
        for ref in refs:
            pk = by_kind[want].get(ref)
            if pk is not None:
                resolved.append(pk.id)
            else:
                missed.append(ref)
        return resolved, missed

    for e in edges:
        a_ids, a_miss = _resolve(e.a_refs, "1H", e.kind, "a")
        if e.kind == "COSY":
            b_ids, b_miss = _resolve(e.b_refs, "1H", e.kind, "b")
        else:
            b_ids, b_miss = _resolve(e.b_refs, "13C", e.kind, "b")
        e.a_peak_ids, e.b_peak_ids = a_ids, b_ids
        e.unresolved = a_miss + b_miss
        for ref in e.unresolved:
            warnings.append(
                f"{e.kind} 边 {e.a_raw}~{e.b_raw} 的引用 {ref} "
                f"无法解析到当前峰（缺失相关边证据，原边保留）")

    context = build_context(docs_meta)
    return Dataset(sources=built_sources, atoms=atoms, peaks=peaks,
                   edges=edges, context=context, warnings=warnings)
