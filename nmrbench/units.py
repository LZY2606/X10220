"""单位、坐标基准与不可变测量值。

所有领域量都携带单位或坐标基准；原始表示原样保留，
派生层才规范化。无法转换的值不丢弃，留在原位并附诊断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ppm 对所有核均为无量纲相对刻度（以参考频率归一化）
SHIFT_UNITS = {"ppm"}
# 仪器频率
FREQ_TO_HZ = {"hz": 1.0, "khz": 1_000.0, "mhz": 1_000_000.0}
# 积分（峰强）：任意强度单位按 1 处理
INTENSITY_TO_COUNTS = {"counts": 1.0, "a.u.": 1.0, "int": 1.0, "": 1.0}
# 耦合常数
COUPLING_TO_HZ = {"hz": 1.0}
# 温度
TEMP_TO_KELVIN = {"k": (1.0, 0.0), "c": (1.0, 273.15)}
# 允许的核种类
NUCLEUS_KINDS = {"1H": "proton", "13C": "carbon"}
REFERENCE_SCALES = {"TMS"}


class UnitError(ValueError):
    """单位无法识别或无法转换到目标基准。"""


@dataclass(frozen=True)
class Measure:
    """带单位/坐标基准的测量值。

    value 为规范化后的数值（float），raw 为原始字符串，unit 为原始单位，
    basis 说明坐标基准（如 ppm@TMS）。unconvertible 为 True 时
    value 为 None，调用方必须保留 raw 并挂诊断。
    """

    raw: str
    value: float | None
    unit: str
    basis: str | None = None
    unconvertible: bool = False
    diagnostic: str | None = None

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "unconvertible": self.unconvertible,
            "diagnostic": self.diagnostic,
        }


def parse_shift(raw: str) -> Measure:
    """解析化学位移：接受 '4.85 ppm'、'4.85ppm'、裸数字（按 ppm）。"""
    token = raw.strip()
    parts = token.split()
    if len(parts) == 1:
        num, unit = parts[0], "ppm"
        # 形如 4.85ppm
        if unit.lower().endswith("ppm"):
            num, unit = token[:-3], "ppm"
    elif len(parts) == 2:
        num, unit = parts
    else:
        return Measure(raw, None, "", None, True, "malformed shift token")
    try:
        value = float(num)
    except ValueError:
        return Measure(raw, None, unit, None, True, f"non-numeric shift '{num}'")
    if unit.lower() not in SHIFT_UNITS:
        return Measure(raw, None, unit, None, True, f"unknown shift unit '{unit}'")
    return Measure(raw, value, "ppm", "ppm@TMS")


def parse_frequency(raw: str) -> Measure:
    """解析仪器频率，规范化到 Hz。"""
    token = raw.strip()
    num, unit = (token.split() + ["mhz"])[:2] if " " not in token else token.split()
    # 处理 400MHz / 400 MHz
    if num and num[-1].isalpha():
        for u in ("mhz", "khz", "hz"):
            if token.lower().endswith(u):
                num = token[: -len(u)].strip()
                unit = u
                break
    try:
        value = float(num)
    except ValueError:
        return Measure(raw, None, unit, None, True, f"non-numeric frequency '{num}'")
    factor = FREQ_TO_HZ.get(unit.lower())
    if factor is None:
        return Measure(raw, None, unit, None, True, f"unknown frequency unit '{unit}'")
    return Measure(raw, value * factor, "Hz", "Hz")


def parse_intensity(raw: str) -> Measure:
    """解析峰强/积分（可无单位）。"""
    token = raw.strip()
    parts = token.split()
    if len(parts) == 1:
        num, unit = parts[0], ""
    else:
        num, unit = parts[0], parts[1]
    try:
        value = float(num)
    except ValueError:
        return Measure(raw, None, unit, None, True, f"non-numeric intensity '{num}'")
    if unit.lower() not in INTENSITY_TO_COUNTS:
        return Measure(raw, None, unit, None, True, f"unknown intensity unit '{unit}'")
    return Measure(raw, value, unit or "a.u.", "counts")


def parse_coupling(raw: str) -> Measure:
    """解析耦合常数（Hz）。"""
    parts = raw.strip().split()
    num = parts[0]
    unit = parts[1] if len(parts) > 1 else "Hz"
    try:
        value = float(num)
    except ValueError:
        return Measure(raw, None, unit, None, True, f"non-numeric coupling '{num}'")
    if unit.lower() not in COUPLING_TO_HZ:
        return Measure(raw, None, unit, None, True, f"unknown coupling unit '{unit}'")
    return Measure(raw, value, "Hz", "Hz")


def parse_temperature(raw: str) -> Measure:
    """解析温度，规范化到 K，同时保留原值与原单位。"""
    parts = raw.strip().split()
    if len(parts) != 2:
        return Measure(raw, None, parts[1] if len(parts) > 1 else "", None, True,
                       "temperature must be '<number> K|C'")
    num, unit = parts
    try:
        raw_value = float(num)
    except ValueError:
        return Measure(raw, None, unit, None, True, f"non-numeric temperature '{num}'")
    spec = TEMP_TO_KELVIN.get(unit.lower())
    if spec is None:
        return Measure(raw, None, unit, None, True, f"unknown temperature unit '{unit}'")
    factor, offset = spec
    return Measure(raw, factor * raw_value + offset, unit, "K")


def parse_nucleus(raw: str) -> str:
    """规范化核种类标签；无法识别时抛出 UnitError。"""
    token = raw.strip()
    if token in NUCLEUS_KINDS:
        return token
    raise UnitError(f"unknown nucleus kind '{raw}' (expected one of {sorted(NUCLEUS_KINDS)})")
