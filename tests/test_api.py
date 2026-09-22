"""End-to-end HTTP workflow through FastAPI: import, candidates, decide,
reopen review, export/import.  Uses a temp DB and the in-process client.
"""
from __future__ import annotations

import importlib
import os
import pathlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("NMRWB_DB", str(db_path))
    import app as app_module
    importlib.reload(app_module)
    from nmrwb import db as dbmod
    app_module.set_clock(dbmod.Clock(1_700_000_000.0))
    return TestClient(app_module.app), app_module


def _create_and_load(c):
    r = c.post("/api/projects", json={"project_id": "P1", "name": "demo"})
    assert r.status_code == 200, r.text
    r = c.post("/api/projects/P1/load-case?actor=reviewer")
    assert r.status_code == 200, r.text
    assert len(r.json()["events"]) == 7


def test_full_review_workflow(client):
    c, _ = client
    _create_and_load(c)

    view = c.get("/api/projects/P1/view").json()
    assert view["run"]["statistics"]["n_nuclei"] == 11
    # provenance on peaks
    h1 = next(p for s in view["parsed"].values()
              for p in s["data"]["peaks"] if p["pid"] == "h1")
    assert h1["shift"]["raw"].endswith("ppm")
    assert h1["shift"]["basis"] == "chemical-shift"
    assert view["parsed"]["S1H"]["meta"]["frequency"] == "400.13MHz"

    # candidates present and selectable with stable ids
    cand = next(cd for n in view["run"]["nuclei"] for cd in n["candidates"]
                if cd["id"] == "C:H1->h1" and not cd["rejected"])
    assert cand["rank"] == 1

    # human accept -> child branch with propagation
    r = c.post("/api/projects/P1/decisions", json={
        "candidate_id": "C:H1->h1", "action": "accept", "actor": "alice",
        "reason": "ethyl CH2", "branch_id": "B:main",
        "create_child_branch": True})
    assert r.status_code == 200, r.text
    decision = r.json()
    assert decision["propagation"]["locked"]["nucleus"] == "H1"

    # reopen review on the child branch: decision persists
    view2 = c.get(f"/api/projects/P1/view?branch={decision['branch_id']}").json()
    decs = view2["decisions_by_candidate"]["C:H1->h1"]
    assert decs[-1]["kind"] == "candidate_accepted"

    # export, delete, import
    bundle = c.get("/api/projects/P1/export").json()
    r = c.post("/api/import-bundle?fresh_database=true", json=bundle)
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == "P1"

    # identity/order survive
    view3 = c.get(f"/api/projects/P1/view?branch={decision['branch_id']}").json()
    assert view3["events"][0]["event_id"] == view["events"][0]["event_id"]
    assert view3["decisions_by_candidate"]["C:H1->h1"][-1]["actor"] == "alice"


def test_conflict_endpoint(client):
    c, _ = client
    _create_and_load(c)
    view = c.get("/api/projects/P1/view").json()
    base = view["events"][-1]["event_id"]
    a = c.post("/api/projects/P1/decisions", json={
        "candidate_id": "C:H2->h2", "action": "accept", "actor": "alice",
        "branch_id": "B:main", "expected_base_event_id": base}).json()
    # bob rejects on alice's branch expecting the stale main base
    b = c.post("/api/projects/P1/decisions", json={
        "candidate_id": "C:H2->h2", "action": "reject", "actor": "bob",
        "branch_id": a["branch_id"], "expected_base_event_id": base}).json()
    assert b["conflict_event_id"]
    r = c.post("/api/projects/P1/conflicts/resolve", json={
        "candidate_id": "C:H2->h2", "resolution": "reject",
        "actor": "chief", "branch_id": b["branch_id"], "reason": "x"})
    assert r.status_code == 200
    assert r.json()["kind"] == "conflict_resolved"


def test_format_error_reports_line(client):
    c, _ = client
    c.post("/api/projects", json={"project_id": "P1", "name": "demo"})
    bad = "@nbr-case/1h\nPEAK h1, onlytwo\n"
    r = c.post("/api/projects/P1/sources", json={
        "source_id": "BAD", "kind": "1h", "filename": "bad.txt", "text": bad})
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 2


def test_revision_returns_local_diff(client):
    c, _ = client
    _create_and_load(c)
    from nmrbench.scenarios import scenario_missing_edge
    from tests.conftest import load_case_text
    new_text = scenario_missing_edge(load_case_text())["HSQC"]
    r = c.post("/api/projects/P1/sources/HSQC/revisions", json={
        "text": new_text, "actor": "curator", "reason": "probe",
        "branch_id": "B:main"})
    assert r.status_code == 200, r.text
    recs = r.json()["changed_records"]
    assert any(x["id"] == "q1" for x in recs)
