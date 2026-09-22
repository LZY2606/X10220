"""领域量解析层。

原则:导入时保留原始表示(raw);派生层才做规范化。
无法转换的值保留在原位置并附诊断,不为了展示而丢弃。
所有领域量都携带单位或坐标基准(ppm / Hz / K / MHz / H / arb)。
"""

from __future__ import annotations

import re
from typing import Any, Dict

_SHIFT_MIN_PPM = -50.0
_SHIFT_MAX_PPM = 300.0


def _diag(code: str, message: str) -> Dict[str, Any]:
    return {"code": code, "message": message}


def parse_shift(raw: Any) -> Dict[str, Any]:
    """化学位移,基准 ppm(相对参考标准,谱仪频率在条件中记录)。"""
    rec = {"raw": raw, "ok": False, "value": None, "unit": "ppm", "diagnostic": None}
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        rec["diagnostic"] = _diag(
            "bad_shift", "化学位移无法解析为 ppm 数值;原值保留在原位置"
        )
        return rec
    if not (_SHIFT_MIN_PPM <= value <= _SHIFT_MAX_PPM):
        rec["diagnostic"] = _diag(
            "shift_out_of_range",
            f"化学位移 {value} ppm 超出常规范围;原值保留在原位置",
        )
        return rec
    rec["ok"] = True
    rec["value"] = value
    return rec


def parse_intensity(raw: Any) -> Dict[str, Any]:
    """峰强,任意单位 arb,派生层只做相对比较。"""
    rec = {"raw": raw, "ok": False, "value": None, "unit": "arb", "diagnostic": None}
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        rec["diagnostic"] = _diag("bad_intensity", "峰强无法解析;原值保留在原位置")
        return rec
    if value < 0:
        rec["diagnostic"] = _diag("negative_intensity", "峰强为负;原值保留在原位置")
        return rec
    rec["ok"] = True
    rec["value"] = value
    return rec


_INTEGRAL_RE = re.compile(r"^(?:ca\.\s*)?(\d+(?:\.\d+)?)\s*[Hh]?$")


def parse_integral(raw: Any) -> Dict[str, Any]:
    """积分,单位 H(质子数),允许 '3'、'3H'、'ca. 2H' 等原始写法。"""
    rec = {"raw": raw, "ok": False, "value": None, "unit": "H", "diagnostic": None}
    match = _INTEGRAL_RE.match(str(raw).strip())
    if not match:
        rec["diagnostic"] = _diag(
            "bad_integral", "积分无法解析(期望如 3 或 3H);原值保留在原位置"
        )
        return rec
    rec["ok"] = True
    rec["value"] = float(match.group(1))
    return rec


_MULT_ALIASES = {
    "s": "s", "singlet": "s",
    "d": "d", "doublet": "d",
    "t": "t", "triplet": "t",
    "q": "q", "quartet": "q",
    "quint": "quint", "sext": "sext", "sept": "sept",
    "m": "m", "multiplet": "m",
    "dd": "dd", "dt": "dt", "td": "td", "tt": "tt",
    "br": "br", "brs": "br",
}


def parse_multiplicity(raw: Any) -> Dict[str, Any]:
    """多重性规范化为标准代码,原始写法保留。"""
    rec = {"raw": raw, "ok": False, "code": None, "unit": "multiplicity_code",
           "diagnostic": None}
    code = _MULT_ALIASES.get(str(raw).strip().lower())
    if code is None:
        rec["diagnostic"] = _diag(
            "bad_multiplicity", "多重性无法识别;原值保留在原位置"
        )
        return rec
    rec["ok"] = True
    rec["code"] = code
    return rec


_TEMP_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(K|°C|C)?$", re.IGNORECASE)


def parse_temperature(raw: Any) -> Dict[str, Any]:
    rec = {"raw": raw, "ok": False, "value": None, "unit": None, "diagnostic": None}
    match = _TEMP_RE.match(str(raw).strip())
    if not match:
        rec["diagnostic"] = _diag("bad_temperature", "温度无法解析;原值保留在原位置")
        return rec
    value = float(match.group(1))
    unit_text = (match.group(2) or "K").upper()
    if unit_text in ("C", "°C"):
        rec["unit"] = "K"
        rec["value"] = round(value + 273.15, 2)
        rec["source_unit"] = "degC"
        rec["source_value"] = value
    else:
        rec["unit"] = "K"
        rec["value"] = value
    rec["ok"] = True
    return rec


_FREQ_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(MHz|GHz)?$", re.IGNORECASE)


def parse_frequency(raw: Any) -> Dict[str, Any]:
    """仪器频率,单位 MHz(GHz 输入换算),保留原始表示。"""
    rec = {"raw": raw, "ok": False, "value": None, "unit": "MHz", "diagnostic": None}
    match = _FREQ_RE.match(str(raw).strip())
    if not match:
        rec["diagnostic"] = _diag("bad_frequency", "仪器频率无法解析;原值保留在原位置")
        return rec
    value = float(match.group(1))
    if (match.group(2) or "MHz").upper() == "GHZ":
        value *= 1000.0
    rec["ok"] = True
    rec["value"] = value
    return rec


_REF_RE = re.compile(r"^(.+?)(?:\s*@\s*(-?\d+(?:\.\d+)?)\s*ppm)?$", re.IGNORECASE)


def parse_reference(raw: Any) -> Dict[str, Any]:
    """参考标准,如 'TMS @ 0.00 ppm';名称与参考位移分开保留。"""
    text = str(raw).strip()
    rec = {"raw": raw, "ok": bool(text), "name": text, "shift_ppm": 0.0,
           "unit": "ppm", "diagnostic": None}
    if not text:
        rec["diagnostic"] = _diag("bad_reference", "参考标准为空;原值保留")
        return rec
    match = _REF_RE.match(text)
    if match:
        rec["name"] = match.group(1).strip()
        if match.group(2) is not None:
            rec["shift_ppm"] = float(match.group(2))
    return rec


def shift_to_hz(shift_ppm: float, frequency_mhz: float) -> float:
    """派生换算 ppm -> Hz;只有在频率可解析时才进行。"""
    return round(shift_ppm * frequency_mhz, 3)
