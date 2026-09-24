"""单位与坐标基准处理。

原则：导入层保留原始表示（raw），规范化只发生在派生层。
无法转换的值不被丢弃，留在原位置并附机器可读的诊断。
"""
from __future__ import annotations

from typing import Any, Optional

CANONICAL_SHIFT_UNIT = "ppm"
CANONICAL_TEMP_UNIT = "K"
CANONICAL_FREQ_UNIT = "MHz"

_KNOWN_REFERENCES = {"TMS", "TSP", "DSS", "residual-solvent", "absolute"}


def _diag(code: str, message: str, raw: Any) -> dict:
    return {"code": code, "message": message, "raw": raw}


def normalize_shift(raw: dict, field_mhz: Optional[float]) -> dict:
    """把化学位移规范化到 ppm（以声明的参考标准为坐标基准）。

    raw 形如 {"value": 7.26, "unit": "ppm", "reference": "TMS"}
    或 {"value": 2904.0, "unit": "Hz", "reference": "TMS"}（需要仪器频率换算）。
    返回 {"canonical": value|None, "unit": "ppm", "diagnostics": [...]}。
    """
    diagnostics: list[dict] = []
    unit = raw.get("unit")
    reference = raw.get("reference")
    value = raw.get("value")
    if value is None or unit is None:
        diagnostics.append(_diag("shift.unparseable", "化学位移缺少数值或单位，保留原始表示", raw))
        return {"canonical": None, "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}
    if reference is None:
        diagnostics.append(_diag("shift.no_reference", "未声明参考标准，坐标基准未知，保留原始表示", raw))
        return {"canonical": None, "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}
    if reference not in _KNOWN_REFERENCES:
        diagnostics.append(_diag("shift.unknown_reference", f"未知参考标准 {reference!r}，无法对齐坐标基准", raw))
        return {"canonical": None, "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}
    if unit == "ppm":
        return {"canonical": float(value), "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}
    if unit == "Hz":
        if not field_mhz:
            diagnostics.append(_diag("shift.hz_without_field", "Hz 表示的位移缺少仪器频率，无法换算 ppm", raw))
            return {"canonical": None, "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}
        return {"canonical": float(value) / float(field_mhz), "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}
    diagnostics.append(_diag("shift.unknown_unit", f"未知位移单位 {unit!r}，保留原始表示", raw))
    return {"canonical": None, "unit": CANONICAL_SHIFT_UNIT, "diagnostics": diagnostics}


def normalize_temperature(raw: dict) -> dict:
    """温度规范化到 K。"""
    diagnostics: list[dict] = []
    unit = raw.get("unit")
    value = raw.get("value")
    if value is None or unit is None:
        diagnostics.append(_diag("temp.unparseable", "温度缺少数值或单位，保留原始表示", raw))
        return {"canonical": None, "unit": CANONICAL_TEMP_UNIT, "diagnostics": diagnostics}
    if unit == "K":
        return {"canonical": float(value), "unit": "K", "diagnostics": diagnostics}
    if unit in ("C", "°C", "degC"):
        return {"canonical": float(value) + 273.15, "unit": "K", "diagnostics": diagnostics}
    diagnostics.append(_diag("temp.unknown_unit", f"未知温度单位 {unit!r}，保留原始表示", raw))
    return {"canonical": None, "unit": CANONICAL_TEMP_UNIT, "diagnostics": diagnostics}


def normalize_frequency(raw: dict) -> dict:
    """仪器频率规范化到 MHz。"""
    diagnostics: list[dict] = []
    unit = raw.get("unit")
    value = raw.get("value")
    if value is None or unit is None:
        diagnostics.append(_diag("freq.unparseable", "频率缺少数值或单位，保留原始表示", raw))
        return {"canonical": None, "unit": CANONICAL_FREQ_UNIT, "diagnostics": diagnostics}
    if unit == "MHz":
        return {"canonical": float(value), "unit": "MHz", "diagnostics": diagnostics}
    if unit == "GHz":
        return {"canonical": float(value) * 1000.0, "unit": "MHz", "diagnostics": diagnostics}
    diagnostics.append(_diag("freq.unknown_unit", f"未知频率单位 {unit!r}，保留原始表示", raw))
    return {"canonical": None, "unit": CANONICAL_FREQ_UNIT, "diagnostics": diagnostics}
