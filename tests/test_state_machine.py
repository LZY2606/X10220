"""Acceptance tests for the six required edge cases + provenance/replay."""
from __future__ import annotations

from nmrwb.parser import parse_source, FormatError
from nmrwb.engine import compute
from nmrbench.scenarios import (CASE_FILES, scenario_overlap, scenario_missing_edge,
                                scenario_bias, scenario_tie, scenario_solvent,
                                scenario_symmetry)
from tests.conftest import load_case_text


def _engine_from_texts(texts):
    sources = {sid: ("v1", parse_source(t, sid, "v1"))
               for sid, t in texts.items()}
    return compute(sources)


def test_overlapping_peaks_not_forced_unique():
    base = load_case_text()
    res = _engine_from_texts(scenario_overlap(base))
    codes = {j["code"] for j in res.judgements}
    assert "OVERLAP_NOT_FORCED" in codes
    # the overlapping group keeps both peaks
    groups = [g for g in res.peak_groups if "h4" in g.group_id and "h2" in g.group_id]
    assert groups
    # both peaks still appear as legal candidates for their respective nuclei
    pids = {c.peak_pid for n in res.nuclei for c in n.candidates if not c.rejected}
    assert {"h2", "h4"} <= pids


def test_symmetry_equivalent_nuclei_preserved():
    base = load_case_text()
    res = _engine_from_texts(scenario_symmetry(base))
    sym = [j for j in res.judgements
           if j["code"] == "SYMMETRY_EQUIVALENCE_PRESERVED"]
    assert any(j["basis"]["equiv_size"] >= 2 for j in sym)
    classes = {j["object"] for j in sym}
    assert {"1H-ortho", "13C-ortho"} & classes


def test_missing_correlation_edge_is_evidence_only():
    base = load_case_text()
    res = _engine_from_texts(scenario_missing_edge(base))
    codes = {j["code"] for j in res.judgements}
    assert "MISSING_EXPECTED_HSQC" in codes
    # H1 must remain eligible even with the HSQC edge removed
    h1 = next(n for n in res.nuclei if n.nucleus == "H1")
    assert any(c.peak_pid == "h1" and not c.rejected for c in h1.candidates)
    # the weak negative evidence is attached, but never a hard rejection
    for n in res.nuclei:
        for c in n.candidates:
            if c.rejection and "EDGE" in (c.rejection.code or ""):
                raise AssertionError("edge absence must never hard-reject")


def test_global_shift_bias_detected_per_dimension():
    base = load_case_text()
    res = _engine_from_texts(scenario_bias(base))
    bias_events = [j for j in res.judgements if j["code"] == "GLOBAL_SHIFT_BIAS"]
    assert bias_events
    assert any(j["basis"]["isotope"] == "1H" for j in bias_events)
    assert res.global_bias["detected"] is True
    # offset is measured, not silently corrected into different candidate ids
    assert res.global_bias["offset_ppm"] >= 0.10


def test_tied_best_candidates_all_retained():
    base = load_case_text()
    res = _engine_from_texts(scenario_tie(base))
    ties = [j for j in res.judgements if j["code"] == "TIED_BEST_CANDIDATES"]
    assert ties
    tied = [(n.nucleus, c) for n in res.nuclei for c in n.candidates
            if c.rank == 1 and c.tie_with]
    assert tied, "expected at least one rank-1 candidate with tie_with"
    # every tied partner is itself rank 1
    for nuc, c in tied:
        for other_id in c.tie_with:
            other = next(x for nn in res.nuclei for x in nn.candidates
                         if x.cand_id == other_id)
            assert other.rank == 1


def test_solvent_change_only_scoped_expiry(loaded_svc):
    # Revise only the 1H source meta (solvent). 13C candidates are untouched.
    before = loaded_svc.snapshot("P1", "B:main")
    run_before = before["current_run"]
    new_text = scenario_solvent(load_case_text())["S1H"]
    out = loaded_svc.revise_source("P1", "S1H", new_text, "curator",
                                   "solvent switch for re-reference check")
    changed = out["event"]["payload"]["changed_records"]
    assert any(c["type"] == "meta" for c in changed)
    after = loaded_svc.snapshot("P1", "B:main")
    # the 13C candidate ids never depend on the 1H source meta change
    affected = {c for ev in after["events"]
                if ev["kind"] == "candidate_expired"
                for c in ev["payload"]["affected_candidates"]}
    assert all(cid.startswith("C:C") is False for cid in affected)
    # historical run remains viewable (not overwritten)
    assert run_before["run_id"] != after["current_run"]["run_id"]


def test_rejected_and_unknown_are_first_class(loaded_svc):
    snap = loaded_svc.snapshot("P1")
    run = snap["current_run"]
    rejected = [c for n in run["nuclei"] for c in n["candidates"] if c["rejected"]]
    assert rejected, "out-of-window candidates must be retained with reasons"
    assert all(c["rejection"]["code"] for c in rejected)


def test_raw_quantities_keep_units_and_unconvertible_diagnostic():
    text = ("@nbr-case/1h\n# solvent: CDCl3\n# frequency: 400.13MHz\n"
            "PEAK h1, 7.20ppm, 0.4 a.u., 2 count, d\n"
            "PEAK hbad, 7.1zzz, 0.3 a.u., 1 count, s\n")
    ps = parse_source(text, "S", "v1")
    assert ps.peaks[0].shift.raw == "7.20ppm"
    assert ps.meta["frequency"] == "400.13MHz"
    assert ps.peaks[1].shift.value is None
    assert any(d.code == "UNCONVERTIBLE_VALUE" for d in ps.diagnostics)


def test_format_error_line_number():
    bad = "@nbr-case/1h\nPEAK h1, onlytwo\n"
    try:
        parse_source(bad, "S", "v1")
        raise AssertionError("expected FormatError")
    except FormatError as e:
        assert e.line == 2
