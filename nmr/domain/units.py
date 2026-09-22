"""领域量的单位与坐标基准处理。

规则（见 README“原始保留 / 派生规范化”）：
- 导入时保留原始表示 ``raw``；派生层才做规范化；
- 无法转换的值不被丢弃：``convertible=False``，值留在原位置并附诊断；
- 化学位移全部携带 ppm 单位与参考基准（TMS, delta scale）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

SHIFT_BASIS = {
    "1H": "delta_ppm_TMS",
    "13C": "delta_ppm_TMS",
}

MULTIPLICITY_CODES = {
    "s": "s", "d": "d", "t": "t", "q": "q", "m": "m",
    "dd": "dd", "dt": "dt", "td": "td", "br s": "br s", "brs": "br s",
}

_FREQ_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)?\s*$", re.I)
_TEMP_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(K|C)\s*$", re.I)


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.match(r"^\s*([-+]?\d+(?:\.\d+)?)\s*$", value)
        if m:
            return float(m.group(1))
    return None


def shift_quantity(raw: Any, nucleus: str, field_name: str = "shift") -> Dict[str, Any]:
    """化学位移：保留原文，规范化为 ppm（相对 TMS 的 delta 标尺）。"""
    num = _to_float(raw)
    basis = SHIFT_BASIS.get(nucleus, "delta_ppm_TMS")
    if num is None:
        return {
            "field": field_name,
            "raw": raw,
            "value": None,
            "unit": "ppm",
            "basis": basis,
            "convertible": False,
            "diagnostic": f"化学位移无法解析为数值，原值保留: {raw!r}",
        }
    return {
        "field": field_name,
        "raw": raw,
        "value": round(num, 6),
        "unit": "ppm",
        "basis": basis,
        "convertible": True,
        "diagnostic": None,
    }


def number_quantity(raw: Any, unit: str, field_name: str) -> Dict[str, Any]:
    num = _to_float(raw)
    if num is None:
        return {
            "field": field_name,
            "raw": raw,
            "value": None,
            "unit": unit,
            "basis": None,
            "convertible": False,
            "diagnostic": f"{field_name} 无法解析为数值，原值保留: {raw!r}",
        }
    return {
        "field": field_name,
        "raw": raw,
        "value": num,
        "unit": unit,
        "basis": None,
        "convertible": True,
        "diagnostic": None,
    }


def frequency_quantity(raw: Any) -> Dict[str, Any]:
    """仪器频率：规范化到 MHz。"""
    text = str(raw).strip()
    m = _FREQ_RE.match(text)
    if not m:
        return {
            "field": "frequency",
            "raw": raw,
            "value": None,
            "unit": "MHz",
            "basis": "spectrometer_frequency",
            "convertible": False,
            "diagnostic": f"仪器频率无法解析，原值保留: {raw!r}",
        }
    value = float(m.group(1))
    unit = (m.group(2) or "MHz").lower()
    if unit == "hz":
        value /= 1e6
    elif unit == "khz":
        value /= 1e3
    return {
        "field": "frequency",
        "raw": raw,
        "value": round(value, 6),
        "unit": "MHz",
        "basis": "spectrometer_frequency",
        "convertible": True,
        "diagnostic": None,
    }


def temperature_quantity(raw: Any) -> Dict[str, Any]:
    """温度：规范化到 K；摄氏度可转换，其他单位失败并保留原值。"""
    text = str(raw).strip()
    m = _TEMP_RE.match(text)
    if not m:
        return {
            "field": "temperature",
            "raw": raw,
            "value": None,
            "unit": "K",
            "basis": "thermodynamic_temperature",
            "convertible": False,
            "diagnostic": f"温度无法解析，原值保留: {raw!r}",
        }
    value = float(m.group(1))
    if m.group(2).upper() == "C":
        value += 273.15
    return {
        "field": "temperature",
        "raw": raw,
        "value": round(value, 3),
        "unit": "K",
        "basis": "thermodynamic_temperature",
        "convertible": True,
        "diagnostic": None,
    }


def multiplicity(raw: Any) -> Dict[str, Any]:
    text = str(raw).strip().lower()
    code = MULTIPLICITY_CODES.get(text)
    return {
        "field": "multiplicity",
        "raw": raw,
        "value": code if code is not None else raw,
        "unit": None,
        "basis": "splitting_code",
        "convertible": code is not None,
        "diagnostic": None if code is not None else f"未知多重性记号，原样保留: {raw!r}",
    }
