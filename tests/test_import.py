"""导入层：原始表示保留、单位基准、明确的格式错误。"""
from nmrb.parser import parse_text


def test_preserves_raw_repr_and_units():
    text = ("@format NMRB-PEAKS1/1\n"
            "#nucleus 1H\n#solvent CDCl3\n#temperature-k 298.15\n"
            "#spectrometer-mhz 500.13\n#reference TMS@0ppm\n"
            "@p1 7.30 100 2.0 d,J=8.0Hz\n"
            "@p2 xx NA 1 br\n")
    doc = parse_text("a.pk1", text)
    assert doc.format == "NMRB-PEAKS1/1"
    recs = {r.label: r for r in doc.records}
    p1 = recs["@p1"]
    assert p1.fields["shift_ppm"] == 7.30
    assert p1.fields["shift_repr"] == "7.30"
    assert p1.fields["shift_unit"] == "ppm"
    assert p1.fields["intensity_unit"] == "counts"
    p2 = recs["@p2"]
    assert p2.fields["shift_ppm"] is None
    assert p2.fields["shift_repr"] == "xx"
    assert p2.fields["intensity"] is None
    assert p2.fields["intensity_repr"] == "NA"
    codes = {d.code for d in doc.diagnostics}
    assert "UNCONVERTIBLE_SHIFT" in codes
    assert "UNCONVERTIBLE_INTENSITY" in codes
    # 无法转换的值留在原位置，不删除记录
    assert len(doc.records) == 2


def test_bad_format_and_bad_experiment():
    doc = parse_text("bad.pk1", "@format NMRB-PEAKS1/9\n@p 1 2 3 s\n")
    assert doc.has_error
    assert any(d.code == "UNKNOWN_FORMAT" for d in doc.diagnostics)

    doc2 = parse_text("bad.peak2",
                      "@format NMRB-PEAKS2/1\n#experiment NOESY\n@e 1 2 strong\n")
    assert any(d.code == "BAD_EXPERIMENT" for d in doc2.diagnostics)


def test_column_count_and_duplicate_labels():
    doc = parse_text("x.pk1",
                     "@format NMRB-PEAKS1/1\n#nucleus 1H\n@a 1 2\n@b 1 2 3 s\n@b 2 3 4 s\n")
    codes = {d.code for d in doc.diagnostics}
    assert "BAD_COLUMN_COUNT" in codes
    assert "DUPLICATE_LABEL" in codes


def test_peak2_and_skeleton_parse():
    doc = parse_text("e.peak2",
                     "@format NMRB-PEAKS2/1\n#experiment HSQC\n#nuclei 1H,13C\n"
                     "@q1 7.3 128.5 strong\n")
    assert doc.records[0].fields["f2_ppm"] == 128.5
    sk = parse_text("s.skel",
                    "@format NMRB-SKELETON/1\n"
                    "@s1 C_A H_A aromatic 7.25ppm group=G1 protons=2\n"
                    "@b1 bond H_A H_B vicinal\n@h1 hmbc H_A C_B x\n")
    assert sk.records[0].fields["protons"] == 2
    assert {r.kind for r in sk.records} == {"site", "bond", "hmbc"}
