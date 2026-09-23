"""案例包文本格式解析。

四种文件:
  .skel   分子骨架(核、等价组、键、预期相关)
  .pk1d   一维 peak list(化学位移/峰强/积分/多重性/J)
  .pk2d   二维相关(COSY/HSQC/HMBC 相关边)
  .params 归属算法参数(算法版本 + 参数指纹来源)

规则:
  - 头部 `key: value` 原样保留(溶剂、仪器频率、参考标准、温度等);
  - 表格以 `表名:` 开始,首行 `|` 分隔列名,随后为数据行;
  - 结构性错误(缺头、列数不符)抛出 FormatError 并带行号;
  - 单个数值无法解析不算致命:原值留在原位置并附诊断。
"""
from __future__ import annotations

from typing import Dict, List, Optional

KINDS = ("skel", "pk1d", "pk2d", "params")

REQUIRED_HEADERS = {
    "skel": ("compound",),
    "pk1d": ("compound", "spectrum", "solvent", "instrument_mhz"),
    "pk2d": ("compound", "spectrum", "solvent", "instrument_mhz"),
    "params": ("algorithm", "algorithm_version"),
}

REQUIRED_TABLES = {
    "skel": {"nuclei": ("id", "element", "expected_ppm")},
    "pk1d": {"peaks": ("id", "shift_ppm", "intensity", "integral", "multiplicity")},
    "pk2d": {"correlations": ("id", "f1_ppm", "f2_ppm", "intensity")},
    "params": {},
}


class FormatError(Exception):
    def __init__(self, message: str, line: Optional[int] = None):
        self.line = line
        if line is not None:
            message = "第 %d 行: %s" % (line, message)
        super().__init__(message)


def parse_text(kind: str, content: str) -> dict:
    if kind not in KINDS:
        raise FormatError("未知文件类型 %r,支持: %s" % (kind, ", ".join(KINDS)))
    header: Dict[str, str] = {}
    tables: Dict[str, dict] = {}
    diagnostics: List[str] = []
    current: Optional[dict] = None
    seen_data = False

    for lineno, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if current is None and not seen_data and ":" in line and not line.endswith(":"):
            key, _, value = line.partition(":")
            key = key.strip()
            if not key:
                raise FormatError("空的头部键", lineno)
            header[key] = value.strip()
            continue
        if line.endswith(":") and "|" not in line:
            name = line[:-1].strip()
            if not name:
                raise FormatError("空的表名", lineno)
            current = {"columns": None, "rows": []}
            tables[name] = current
            seen_data = True
            continue
        if current is None:
            raise FormatError("表格之外出现数据: %r" % line, lineno)
        cells = [c.strip() for c in line.split("|")]
        if current["columns"] is None:
            if any(not c for c in cells):
                raise FormatError("列名不能为空", lineno)
            current["columns"] = cells
            continue
        if len(cells) != len(current["columns"]):
            raise FormatError(
                "列数 %d 与表头 %d 不符" % (len(cells), len(current["columns"])), lineno
            )
        current["rows"].append(dict(zip(current["columns"], cells)))

    for req in REQUIRED_HEADERS[kind]:
        if req not in header:
            raise FormatError("缺少必需头部 %r" % req)
    for table, cols in REQUIRED_TABLES[kind].items():
        if table not in tables:
            raise FormatError("缺少必需表格 %r" % table)
        have = tables[table]["columns"] or []
        for col in cols:
            if col not in have:
                raise FormatError("表格 %r 缺少必需列 %r" % (table, col))
    return {"kind": kind, "header": header, "tables": tables, "diagnostics": diagnostics}
