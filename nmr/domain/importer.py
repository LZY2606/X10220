"""源文件导入层。

接受六类 JSON 源文件：
- ``1h_peaks`` / ``13c_peaks``：一维 peak list；
- ``hsqc`` / ``cosy`` / ``hmbc``：二维相关 peak list（边）；
- ``skeleton``：分子骨架（原子 + 键 + 对称组）；
- ``experiment_meta``：溶剂、温度、仪器频率、参考标准。

硬性格式错误（坏 JSON、未知类型、缺字段、重复 id）拒绝提交并返回明确诊断；
值级问题（无法转换的数字/单位）不丢数据，作为软诊断随原值保留。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from . import units as U

VALID_KINDS = {
    "1h_peaks", "13c_peaks", "hsqc", "cosy", "hmbc", "skeleton", "experiment_meta"
}
NUCLEUS_OF = {"1h_peaks": "1H", "13c_peaks": "13C"}
EXPERIMENT_OF = {"hsqc": "HSQC", "cosy": "COSY", "hmbc": "HMBC"}


class FormatError(ValueError):
    def __init__(self, errors: List[Dict[str, Any]]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def _error(code: str, message: str, field: str = None, file_index: int = None,
           raw: Any = None) -> Dict[str, Any]:
    return {"severity": "error", "code": code, "message": message,
            "field": field, "file_index": file_index, "raw": raw}


def warning(code: str, message: str, field: str = None, file_index: int = None,
            raw: Any = None) -> Dict[str, Any]:
    return {"severity": "warning", "code": code, "message": message,
            "field": field, "file_index": file_index, "raw": raw}


def parse_source_text(content: str, declared_kind: str = None,
                      declared_filename: str = None) -> Dict[str, Any]:
    """解析一个源文件文本；结构错误抛 :class:`FormatError`。"""
    try:
        doc = json.loads(content)
    except json.JSONDecodeError as exc:
        raise FormatError([_error("bad_json", f"JSON 解析失败: {exc.msg}",
                                  field=f"line {exc.lineno} col {exc.colno}")])
    if not isinstance(doc, dict):
        raise FormatError([_error("not_object", "源文件顶层必须是 JSON 对象")])

    kind = doc.get("kind") or declared_kind
    if kind not in VALID_KINDS:
        raise FormatError([_error("unknown_kind",
                                  f"未知或缺失的源类型 kind={kind!r}，合法值: {sorted(VALID_KINDS)}",
                                  field="kind", raw=kind)])
    filename = doc.get("filename") or declared_filename or f"{kind}.json"
    file_version = int(doc.get("file_version", 1))

    parser = {
        "1h_peaks": _parse_peaks, "13c_peaks": _parse_peaks,
        "hsqc": _parse_edges, "cosy": _parse_edges, "hmbc": _parse_edges,
        "skeleton": _parse_skeleton, "experiment_meta": _parse_meta,
    }[kind]
    body, diagnostics = parser(doc, kind)
    return {
        "kind": kind,
        "filename": filename,
        "file_version": file_version,
        "nucleus": NUCLEUS_OF.get(kind),
        "experiment": EXPERIMENT_OF.get(kind),
        "raw_text": content,
        "raw_doc": doc,
        "body": body,
        "diagnostics": diagnostics,
    }


def _require_rows(doc: Dict[str, Any], kind: str) -> List[Any]:
    rows = doc.get("rows")
    if not isinstance(rows, list) or not rows:
        raise FormatError([_error("missing_rows", f"{kind} 必须包含非空 rows 数组",
                                  field="rows")])
    return rows


def _row_key(row: Dict[str, Any], index: int) -> str:
    key = row.get("id")
    if key is None:
        return f"r{index + 1}"
    return str(key)


def _check_unique(keys: List[str], kind: str) -> None:
    seen: Dict[str, int] = {}
    errors: List[Dict[str, Any]] = []
    for i, key in enumerate(keys):
        if key in seen:
            errors.append(_error("duplicate_id",
                                 f"{kind} 中出现重复 id={key!r}（行 {seen[key] + 1} 与 {i + 1}）",
                                 field="id", file_index=i, raw=key))
        else:
            seen[key] = i
    if errors:
        raise FormatError(errors)


def _parse_peaks(doc: Dict[str, Any], kind: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    nucleus = NUCLEUS_OF[kind]
    rows = _require_rows(doc, kind)
    diagnostics: List[Dict[str, Any]] = []
    peaks: List[Dict[str, Any]] = []
    keys: List[str] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FormatError([_error("row_not_object", f"第 {i + 1} 行必须是对象",
                                      file_index=i, raw=row)])
        if "shift" not in row:
            raise FormatError([_error("missing_field",
                                      f"{kind} 第 {i + 1} 行缺少必填字段 shift",
                                      field="shift", file_index=i)])
        key = _row_key(row, i)
        keys.append(key)
        shift = U.shift_quantity(row["shift"], nucleus)
        if not shift["convertible"]:
            diagnostics.append(warning("unconvertible_shift", shift["diagnostic"],
                                       field="shift", file_index=i, raw=row["shift"]))
        intensity = U.number_quantity(row.get("intensity"), "counts", "intensity") \
            if row.get("intensity") is not None else None
        if intensity and not intensity["convertible"]:
            diagnostics.append(warning("unconvertible_intensity", intensity["diagnostic"],
                                       field="intensity", file_index=i, raw=row["intensity"]))
        integration = U.number_quantity(row.get("integration"), "protons", "integration") \
            if row.get("integration") is not None else None
        if integration and not integration["convertible"]:
            diagnostics.append(warning("unconvertible_integration", integration["diagnostic"],
                                       field="integration", file_index=i,
                                       raw=row["integration"]))
        mult = U.multiplicity(row["multiplicity"]) if row.get("multiplicity") is not None else None
        if mult and not mult["convertible"]:
            diagnostics.append(warning("unknown_multiplicity", mult["diagnostic"],
                                       field="multiplicity", file_index=i,
                                       raw=row["multiplicity"]))
        peaks.append({
            "file_index": i,
            "key": key,
            "nucleus": nucleus,
            "shift": shift,
            "intensity": intensity,
            "integration": integration,
            "multiplicity": mult,
            "raw_row": row,
        })
    _check_unique(keys, kind)
    return {"peaks": peaks}, diagnostics


def _parse_edges(doc: Dict[str, Any], kind: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    rows = _require_rows(doc, kind)
    experiment = EXPERIMENT_OF[kind]
    diagnostics: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FormatError([_error("row_not_object", f"第 {i + 1} 行必须是对象",
                                      file_index=i, raw=row)])
        missing = [f for f in ("from", "to") if f not in row]
        if missing:
            raise FormatError([_error("missing_field",
                                      f"{kind} 第 {i + 1} 行缺少字段 {missing}",
                                      field=",".join(missing), file_index=i)])
        edge = {
            "file_index": i,
            "key": str(row.get("id") or f"r{i + 1}"),
            "experiment": experiment,
            "from_ref": str(row["from"]),
            "to_ref": str(row["to"]),
            "from_axis": U.shift_quantity(row["from_ppm"], "1H", "from_ppm")
            if row.get("from_ppm") is not None else None,
            "to_axis": U.shift_quantity(row["to_ppm"],
                                        "13C" if experiment == "HSQC" else "1H",
                                        "to_ppm")
            if row.get("to_ppm") is not None else None,
            "intensity": U.number_quantity(row["intensity"], "counts", "intensity")
            if row.get("intensity") is not None else None,
            "raw_row": row,
        }
        if edge["from_axis"] and not edge["from_axis"]["convertible"]:
            diagnostics.append(warning("unconvertible_axis", edge["from_axis"]["diagnostic"],
                                       field="from_ppm", file_index=i))
        if edge["to_axis"] and not edge["to_axis"]["convertible"]:
            diagnostics.append(warning("unconvertible_axis", edge["to_axis"]["diagnostic"],
                                       field="to_ppm", file_index=i))
        edges.append(edge)
    return {"edges": edges}, diagnostics


def _parse_skeleton(doc: Dict[str, Any], kind: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    atoms_raw = doc.get("atoms")
    if not isinstance(atoms_raw, list) or not atoms_raw:
        raise FormatError([_error("missing_atoms", "skeleton 必须包含非空 atoms 数组",
                                  field="atoms")])
    bonds_raw = doc.get("bonds", [])
    if not isinstance(bonds_raw, list):
        raise FormatError([_error("bad_bonds", "skeleton.bonds 必须是数组", field="bonds")])

    atoms: List[Dict[str, Any]] = []
    labels: List[str] = []
    diagnostics: List[Dict[str, Any]] = []
    for i, row in enumerate(atoms_raw):
        if not isinstance(row, dict):
            raise FormatError([_error("row_not_object", f"atom 第 {i + 1} 行必须是对象",
                                      file_index=i, raw=row)])
        for field in ("label", "element", "nucleus"):
            if field not in row:
                raise FormatError([_error("missing_field",
                                          f"atom 第 {i + 1} 行缺少字段 {field}",
                                          field=field, file_index=i)])
        if row["nucleus"] not in ("1H", "13C"):
            raise FormatError([_error("bad_nucleus",
                                      f"atom {row.get('label')!r} nucleus 必须为 1H 或 13C",
                                      field="nucleus", file_index=i, raw=row["nucleus"])])
        labels.append(str(row["label"]))
        if "predicted_shift" not in row:
            raise FormatError([_error("missing_field",
                                      f"atom {row['label']} 缺少 predicted_shift（ppm）",
                                      field="predicted_shift", file_index=i)])
        pred = U.shift_quantity(row["predicted_shift"], row["nucleus"], "predicted_shift")
        if not pred["convertible"]:
            diagnostics.append(warning("unconvertible_predicted_shift", pred["diagnostic"],
                                       field="predicted_shift", file_index=i))
        atoms.append({
            "file_index": i,
            "label": str(row["label"]),
            "element": str(row["element"]),
            "nucleus": row["nucleus"],
            "symmetry_group": str(row.get("symmetry_group", f"sg_{row['label']}")),
            "equivalent_to": str(row["equivalent_to"]) if row.get("equivalent_to") else None,
            "attached_to": str(row["attached_to"]) if row.get("attached_to") else None,
            "protons": int(row["protons"]) if row.get("protons") is not None else None,
            "predicted_shift": pred,
            "raw_row": row,
        })
    _check_unique(labels, "skeleton atoms")

    bonds: List[Dict[str, Any]] = []
    label_set = set(labels)
    for i, row in enumerate(bonds_raw):
        if not isinstance(row, dict) or "from" not in row or "to" not in row:
            raise FormatError([_error("bad_bond",
                                      f"bond 第 {i + 1} 行必须是含 from/to 的对象",
                                      file_index=i, raw=row)])
        a, b = str(row["from"]), str(row["to"])
        for node in (a, b):
            if node not in label_set:
                raise FormatError([_error("dangling_bond",
                                          f"bond 引用了不存在的原子 {node}",
                                          field="from/to", file_index=i, raw=row)])
        bonds.append({"file_index": i, "from": a, "to": b,
                      "order": int(row.get("order", 1)), "raw_row": row})
    expected_raw = doc.get("expected_coupling", {})
    if expected_raw and not isinstance(expected_raw, dict):
        raise FormatError([_error("bad_expected_coupling",
                                  "skeleton.expected_coupling 必须是对象",
                                  field="expected_coupling")])
    expected_coupling: Dict[str, List[List[str]]] = {}
    for experiment in ("COSY", "HSQC", "HMBC"):
        pairs = expected_raw.get(experiment, [])
        if not isinstance(pairs, list):
            raise FormatError([_error("bad_expected_coupling",
                                      f"expected_coupling.{experiment} 必须是数组",
                                      field=f"expected_coupling.{experiment}")])
        clean = []
        for i, pair in enumerate(pairs):
            if not isinstance(pair, list) or len(pair) != 2:
                raise FormatError([_error("bad_expected_coupling",
                                          f"expected_coupling.{experiment}[{i}] 必须是 [a,b]",
                                          file_index=i, raw=pair)])
            a, b = str(pair[0]), str(pair[1])
            for node in (a, b):
                if node not in label_set:
                    raise FormatError([_error("dangling_expected_coupling",
                                              f"expected_coupling 引用了不存在的原子 {node}",
                                              file_index=i, raw=pair)])
            clean.append([a, b])
        expected_coupling[experiment] = clean
    return {"atoms": atoms, "bonds": bonds,
            "expected_coupling": expected_coupling}, diagnostics


def _parse_meta(doc: Dict[str, Any], kind: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    for field in ("solvent", "frequency"):
        if field not in doc:
            raise FormatError([_error("missing_field",
                                      f"experiment_meta 缺少必填字段 {field}",
                                      field=field)])
    diagnostics: List[Dict[str, Any]] = []
    freq = U.frequency_quantity(doc["frequency"])
    if not freq["convertible"]:
        diagnostics.append(warning("unconvertible_frequency", freq["diagnostic"],
                                   field="frequency", raw=doc["frequency"]))
    temp = U.temperature_quantity(doc["temperature"]) if "temperature" in doc else None
    if temp and not temp["convertible"]:
        diagnostics.append(warning("unconvertible_temperature", temp["diagnostic"],
                                   field="temperature", raw=doc["temperature"]))
    meta = {
        "nucleus": doc.get("nucleus", "1H"),
        "solvent": {"raw": doc["solvent"], "value": str(doc["solvent"]),
                    "basis": "solvent_name"},
        "temperature": temp,
        "frequency": freq,
        "reference": {"raw": doc.get("reference", "TMS"),
                      "value": str(doc.get("reference", "TMS")),
                      "basis": "chemical_shift_reference"},
        "raw_doc": {k: v for k, v in doc.items() if k != "rows"},
    }
    return {"meta": meta}, diagnostics
