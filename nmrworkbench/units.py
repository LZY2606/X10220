"""领域量:一律携带单位/坐标基准;原始表示保留,派生层才规范化。

无法转换的值留在原位置并附诊断,不为画图丢弃。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

KNOWN_UNITS = ("ppm", "Hz", "MHz", "K")

_NUM = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z_]*)\s*$")


@dataclass
class Quantity:
    raw: str
    value: Optional[float]
    unit: Optional[str]
    status: str  # ok | absent | unparsed
    diagnostic: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "diagnostic": self.diagnostic,
        }


def parse_quantity(raw, expected_unit: Optional[str] = None) -> Quantity:
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return Quantity(raw=text, value=None, unit=expected_unit, status="absent")
    m = _NUM.match(text)
    if not m:
        return Quantity(
            raw=text,
            value=None,
            unit=expected_unit,
            status="unparsed",
            diagnostic="无法解析为数值,原始表示保留: %r" % text,
        )
    value = float(m.group(1))
    unit = m.group(2) or expected_unit
    if expected_unit and unit and unit != expected_unit:
        return Quantity(
            raw=text,
            value=value,
            unit=unit,
            status="unparsed",
            diagnostic="单位 %r 与期望 %r 不一致,值保留但不参与规范化" % (unit, expected_unit),
        )
    return Quantity(raw=text, value=value, unit=unit, status="ok")
