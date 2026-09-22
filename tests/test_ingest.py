import pytest

from nmrwb.ingest import IngestError, parse_input

H1 = """# nmrwb-1d v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
temperature_k: 298.0
reference: TMS
peak 0.86 1000 6.0 d
peak 0.8x 500 1.0 s
"""


def test_raw_representation_preserved():
    r = parse_input("h1", "nmrwb-1d", H1)
    p = r["parsed"]
    assert p["meta"]["solvent"] == "CDCl3"          # 原始字符串保留
    assert p["meta"]["frequency_mhz"] == "400.13"
    assert p["peaks"][0]["shift_raw"] == "0.86"
    assert p["peaks"][0]["shift"] == 0.86            # 派生层才规范化


def test_unconvertible_value_stays_in_place_with_diagnostic():
    r = parse_input("h1", "nmrwb-1d", H1)
    p = r["parsed"]
    bad = p["peaks"][1]
    assert bad["shift"] is None
    assert bad["shift_raw"] == "0.8x"                # 原值留在原位置
    assert any(d["field"] == "shift" and d["line"] == 8
               for d in r["diagnostics"])


def test_explicit_format_errors():
    with pytest.raises(IngestError):
        parse_input("x", "nmrwb-9d", H1)             # 未知格式
    with pytest.raises(IngestError):
        parse_input("h1", "nmrwb-1d", "peak 1.0 1 1 s")  # 缺文件头
    with pytest.raises(IngestError):
        parse_input("h1", "nmrwb-2d", H1)            # 文件头与声明不符
