import pytest

from nmrworkbench.importer import parse_document
from nmrworkbench.units import parse_quantity, condition_fingerprint


ONE_D = """
@document h1
@kind 1d
@version 1
@nucleus 1H
@solvent CDCl3
@temperature 25 C
@reference TMS 0.0 ppm
@spectrometer_frequency 400.13 MHz
@columns id shift intensity integral multiplicity j_hz
peak Hp1 1.234 ppm 1000 a.u. 3.0 H-equiv d 7.1 Hz
"""


def test_quantities_keep_raw_and_canonical():
    q = parse_quantity("400.13 MHz", "frequency")
    assert q.raw == "400.13 MHz"
    assert q.canonical_value == pytest.approx(400.13)
    assert q.canonical_unit == "MHz"
    t = parse_quantity("25 C", "temperature")
    assert t.canonical_value == pytest.approx(298.15)
    bad = parse_quantity("1.2 furlong", "chemical_shift")
    assert bad.value == 1.2
    assert bad.canonical_value is None
    assert any(d.code == "BAD_SHIFT_UNIT" for d in bad.diagnostics)


def test_import_preserves_all_fields_and_basis():
    doc = parse_document(ONE_D, "h1.nmr")
    assert doc.accepted
    assert doc.meta["solvent"] == "CDCl3"
    assert doc.meta["spectrometer_frequency_mhz"] == pytest.approx(400.13)
    p = doc.peaks[0]
    assert p.shift.raw == "1.234 ppm"
    assert p.shift.basis == "TMS 0.0 ppm"
    assert p.integral.canonical_value == 3.0
    assert p.multiplicity == "d"
    assert p.j_couplings[0].canonical_value == pytest.approx(7.1)
    assert len(doc.raw_text) > 0 and doc.content_hash


@pytest.mark.parametrize("text,code", [
    ("@document x\n@kind spectrum\n", "UNKNOWN_KIND"),
    ("@document x\n@kind 1d\npeak H 1 ppm\n", "MISSING_NUCLEUS"),
    ("@document x\n@kind 1d\n@nucleus 1H\nfrobnicate x y\n", "UNKNOWN_ROW"),
])
def test_explicit_format_errors_are_rejected(text, code):
    doc = parse_document(text, "bad.nmr")
    assert not doc.accepted
    assert code in [d.code for d in doc.diagnostics]
    # raw text retained even when rejected
    assert doc.raw_text.strip() != ""


def test_unconvertible_value_stays_in_place_with_diagnostic():
    text = ("@document x\n@kind 1d\n@nucleus 1H\n"
            "peak Hp9 1.0 furlong 100 a.u.\n")
    doc = parse_document(text, "soft.nmr")
    assert doc.accepted
    p = doc.peaks[0]
    assert p.shift.canonical_value is None
    assert p.shift.raw == "1.0 furlong"
    assert any(d.severity == "warning" for d in p.shift.diagnostics)


def test_condition_fingerprint_changes_on_solvent_change():
    a = condition_fingerprint({"solvent": "CDCl3", "reference": "TMS"})
    b = condition_fingerprint({"solvent": "DMSO-d6", "reference": "TMS"})
    assert a != b
