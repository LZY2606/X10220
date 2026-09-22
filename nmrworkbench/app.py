"""FastAPI application: review workbench over the event-sourced service."""
from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .store import EventStore, Clock
from .service import WorkbenchService
from .importer import parse_document
from .casepack import build_case_project, load_manifest, CASE_ROOT
from . import render

DB_PATH = os.environ.get(
    "NMR_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "nmr_workbench.sqlite3"))


def get_store():
    return EventStore(DB_PATH, clock=Clock(start=1_700_000_000.0, step=0.0))


app = FastAPI(title="NMR Assignment Candidate Workbench", version="1.0.0")
_STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


class ProjectIn(BaseModel):
    project_id: str
    name: str


class SourceIn(BaseModel):
    logical_id: str
    filename: str = "pasted.nmr"
    raw_text: str
    claimed_version: Optional[str] = None
    author: str = "importer"
    branch: str = "main"


class ParamIn(BaseModel):
    parameters: dict
    reason: str = ""
    author: str = "reviewer"


class DecisionIn(BaseModel):
    nucleus_id: str
    verdict: str
    peak_id: Optional[str] = None
    reason: str = ""
    author: str = "reviewer"
    branch: str = "main"
    parent_event: Optional[str] = None


class ResolveIn(BaseModel):
    nucleus_id: str
    verdict: str
    peak_id: str
    winner_event: str
    loser_event: str
    author: str = "lead-reviewer"


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(_STATIC, "index.html"), encoding="utf-8") as fh:
        return fh.read()


@app.get("/api/health")
def health():
    return {"ok": True, "db": os.path.basename(DB_PATH)}


@app.get("/api/projects")
def list_projects():
    store = get_store()
    try:
        return {"projects": store.list_projects()}
    finally:
        store.close()


@app.post("/api/projects")
def create_project(body: ProjectIn):
    store = get_store()
    try:
        store.create_project(body.project_id, body.name)
        return {"project_id": body.project_id}
    finally:
        store.close()


@app.post("/api/projects/{pid}/sources")
def submit_source(pid: str, body: SourceIn):
    store = get_store()
    try:
        svc = WorkbenchService(store)
        if not store.get_project(pid):
            raise HTTPException(404, "unknown project")
        ev, doc = svc.submit_source(
            pid, body.logical_id, body.raw_text, body.filename,
            author=body.author, branch=body.branch,
            claimed_version=body.claimed_version)
        return {"event": ev, "document": doc.to_json()}
    finally:
        store.close()


@app.post("/api/projects/{pid}/parameters")
def set_parameters(pid: str, body: ParamIn):
    store = get_store()
    try:
        svc = WorkbenchService(store)
        ev = svc.set_parameters(pid, body.parameters, body.reason, body.author)
        return {"event": ev}
    finally:
        store.close()


@app.post("/api/projects/{pid}/decisions")
def decide(pid: str, body: DecisionIn):
    store = get_store()
    try:
        svc = WorkbenchService(store)
        ev, new_branch, conflict = svc.decide(
            pid, body.nucleus_id, body.verdict, body.peak_id, body.reason,
            author=body.author, branch=body.branch,
            parent_event=body.parent_event)
        return {"event": ev, "branch": new_branch or body.branch,
                "forked": new_branch is not None,
                "conflict_prior": conflict}
    finally:
        store.close()


@app.post("/api/projects/{pid}/resolve-conflict")
def resolve(pid: str, body: ResolveIn):
    store = get_store()
    try:
        svc = WorkbenchService(store)
        ev = svc.resolve_conflict(
            pid, body.nucleus_id, body.verdict, body.peak_id,
            winner_event=body.winner_event, loser_event=body.loser_event,
            author=body.author)
        return {"event": ev}
    finally:
        store.close()


@app.get("/api/projects/{pid}/view")
def view(pid: str, branch: str = "main", upto_seq: Optional[int] = None):
    store = get_store()
    try:
        svc = WorkbenchService(store)
        return svc.view(pid, branch=branch, upto_seq=upto_seq)
    finally:
        store.close()


@app.get("/api/projects/{pid}/events")
def events(pid: str):
    store = get_store()
    try:
        out = {}
        for br in store.branches(pid):
            out[br] = store.events_for_branch(pid, br)
        return {"project_id": pid, "branches": out}
    finally:
        store.close()


@app.get("/api/projects/{pid}/svg/{which}")
def svg(pid: str, which: str, branch: str = "main", upto_seq: Optional[int] = None):
    store = get_store()
    try:
        svc = WorkbenchService(store)
        v = svc.view(pid, branch=branch, upto_seq=upto_seq)
        if not v.get("analysis"):
            return PlainTextResponse(
                '<p class="empty">No sources imported on this branch yet.</p>')
        if which == "molecule":
            return HTMLResponse(render.molecule_svg(v))
        if which == "peaks":
            return HTMLResponse(render.peak_table_svg(v))
        if which == "edges":
            return HTMLResponse(render.edge_table_svg(v))
        if which == "candidates":
            return HTMLResponse(render.candidate_table(v))
        raise HTTPException(404, "unknown svg view")
    finally:
        store.close()


@app.get("/api/projects/{pid}/export")
def export(pid: str):
    store = get_store()
    try:
        return store.export_project(pid)
    finally:
        store.close()


@app.post("/api/import-bundle")
def import_bundle(bundle: dict):
    store = get_store()
    try:
        pid = store.import_project(bundle, conflict="error")
        return {"imported": pid, "events": len(bundle["events"])}
    finally:
        store.close()


@app.post("/api/cases/load")
def load_case():
    store = get_store()
    try:
        info = build_case_project(store)
        return {"project_id": info["project_id"],
                "baseline_events": info["baseline_events"],
                "editor_a_event": info["editor_a_event"],
                "editor_b_event": info["editor_b_event"],
                "conflict_branch": info["conflict_branch"],
                "resolution_event": info["resolution_event"],
                "condition_event": info["condition_event"]}
    finally:
        store.close()


@app.get("/api/cases/manifest")
def case_manifest():
    return load_manifest()
