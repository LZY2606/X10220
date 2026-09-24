"""FastAPI entrypoint for the NMR assignment workbench.

Run: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5340
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nmrwb import casepack
from nmrwb.service import MAIN, Service

DB_PATH = str(Path(__file__).parent / "nmrwb.db")
STATIC = Path(__file__).parent / "nmrwb" / "static"

svc = Service(DB_PATH)
app = FastAPI(title="NMR Assignment Workbench")


class ImportBody(BaseModel):
    doc_id: str
    format: str
    raw: str
    actor: str = "reviewer"


class ReviseDocBody(BaseModel):
    doc_id: str
    raw: str
    actor: str = "reviewer"


class ParamsBody(BaseModel):
    changes: dict
    affects: list[str]
    actor: str = "reviewer"


class RunBody(BaseModel):
    branch: str = MAIN
    actor: str = "reviewer"


class DecideBody(BaseModel):
    branch: str = MAIN
    peak_id: str
    atom_id: str
    action: str
    actor: str = "reviewer"


class ResolveBody(BaseModel):
    conflict_id: str
    decision_id: str
    actor: str = "reviewer"


class UndoBody(BaseModel):
    decision_id: str
    actor: str = "reviewer"


class ImportProjectBody(BaseModel):
    data: dict


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state(branch: str = MAIN, at_seq: int | None = None):
    return svc.state(branch, at_seq)


@app.post("/api/casepack")
def load_casepack():
    return casepack.build_case(svc)


@app.post("/api/import")
def import_doc(body: ImportBody):
    return svc.import_document(body.doc_id, body.format, body.raw, body.actor)


@app.post("/api/document/revise")
def revise_doc(body: ReviseDocBody):
    doc = svc.latest_docs().get(body.doc_id)
    if doc is None:
        return JSONResponse({"error": "unknown doc_id"}, status_code=404)
    return svc.import_document(body.doc_id, doc["format"], body.raw, body.actor)


@app.post("/api/params/revise")
def revise_params(body: ParamsBody):
    return svc.commit_params(body.changes, body.affects, body.actor)


@app.post("/api/run")
def run(body: RunBody):
    return svc.run_assignment(body.branch, body.actor)


@app.post("/api/decide")
def decide(body: DecideBody):
    return svc.decide(body.branch, body.peak_id, body.atom_id, body.action, body.actor)


@app.post("/api/conflict/resolve")
def resolve(body: ResolveBody):
    return svc.resolve_conflict(body.conflict_id, body.decision_id, body.actor)


@app.post("/api/decision/undo")
def undo(body: UndoBody):
    return svc.undo_decision(body.decision_id, body.actor)


@app.get("/api/export")
def export():
    return svc.export()


@app.post("/api/project/import")
def import_project(body: ImportProjectBody):
    return svc.import_events(body.data)


@app.post("/api/reset")
def reset():
    global svc
    svc.store.wipe()
    svc = Service(DB_PATH)
    return {"reset": True}
