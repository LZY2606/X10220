"""The shipped counter-evidence archive must exercise every required case and
be reproducible from the case pack with the fixed seed."""
import json
import pathlib

from nmrbench.run_archive import build_archive, write_archive

CASE = pathlib.Path("data/case001")


def test_archive_scenarios_present_and_asserted():
    a = build_archive(CASE)
    assert a["seed"] == 20260919
    names = set(a["scenarios"])
    assert {"overlap", "symmetry", "missing_edge", "global_bias",
            "tied_best", "solvent_meta"} <= names
    for name, sc in a["scenarios"].items():
        for code in sc["expect_codes"]:
            assert code in sc["matched_codes"], (name, code)
        # adopted and not-adopted results both retained
        assert sc["retained"]["eligible_candidates"] > 0
        assert sc["retained"]["rejected_candidates"] > 0


def test_archive_includes_machine_event_histories():
    a = build_archive(CASE)
    h = a["histories"]
    assert h["two_editor_conflict"]["machine_event"]["code"] == \
        "TWO_EDITOR_CONFLICT"
    assert h["two_editor_conflict"]["machine_event"]["resolution_required"]
    assert h["parameter_revision"]["tied_candidates"]
    assert h["local_source_edit"]["weak_evidence"]


def test_archive_is_byte_stable(tmp_path):
    p1 = write_archive(CASE, tmp_path / "a")
    p2 = write_archive(CASE, tmp_path / "b")
    assert json.loads(p1.read_text()) == json.loads(p2.read_text())


def test_shipped_archive_matches_fresh_build(tmp_path):
    shipped = pathlib.Path("nmrbench/generated/archive.json")
    fresh = build_archive(CASE)
    if shipped.exists():
        assert json.loads(shipped.read_text()) == fresh
