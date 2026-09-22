"""NMR 共振经纬工作台 — FastAPI 入口。

演示命令：.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5340
"""
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nmrwb import casepack, domain
from nmrwb.engine import run_compute
from nmrwb.parsing import ParseError
from nmrwb.store import MAIN_BRANCH, Store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


class InputBody(BaseModel):
    kind: str
    label: str = ""
    raw_text: str
    actor: str = "anonymous"
    parent_id: str = None


class ComputeBody(BaseModel):
    branch_id: str = MAIN_BRANCH
    actor: str = "anonymous"


class DecisionBody(BaseModel):
    nucleus: str
    cand_id: str
    action: str
    actor: str = "anonymous"
    note: str = None
    branch_id: str = MAIN_BRANCH


class ResolveBody(BaseModel):
    nucleus: str
    chosen_decision_id: str
    actor: str = "reviewer"


def _err(exc, code=400):
    raise HTTPException(status_code=code, detail=str(exc))


def create_app(db_path=None):
    db_path = db_path or os.environ.get("NMRWB_DB") \
        or str(BASE_DIR / "workbench.db")
    store = Store(db_path)
    app = FastAPI(title="NMR 共振经纬工作台")
    app.state.store = store

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state(branch_id: str = MAIN_BRANCH):
        heads = store.head_inputs()
        head_ids = {h["input_id"] for h in heads}
        peaks, edges, atoms, bonds, params = [], [], [], [], None
        for h in heads:
            if h["kind"] == "peaks1d":
                peaks += store.peaks_of(h["input_id"])
            elif h["kind"] == "corr2d":
                edges += store.edges_of(h["input_id"])
            elif h["kind"] == "params":
                params = store.params_of(h["input_id"])
                params["input_id"] = h["input_id"]
            elif h["kind"] == "molecule":
                atoms = store.atoms_of(h["input_id"])
                bonds = store.bonds_of(h["input_id"])
        cands = []
        for c in store.candidates_all():
            row = dict(c)
            row["status"] = store.resolve_status(c, branch_id)
            cands.append(row)
        return {
            "branch_id": branch_id,
            "branches": store.branches_all(),
            "inputs": [{**i, "is_head": i["input_id"] in head_ids}
                       for i in store.all_inputs()],
            "peaks": peaks, "edges": edges, "atoms": atoms, "bonds": bonds,
            "params": params,
            "candidates": cands,
            "decisions": store.decisions_all(),
            "events": store.events_all(),
        }

    @app.post("/api/inputs", status_code=201)
    def commit_input(body: InputBody):
        try:
            inp, diags = domain.commit_input(
                store, body.kind, body.label, body.raw_text,
                body.actor, body.parent_id)
        except ParseError as exc:
            _err(exc, 422)
        except ValueError as exc:
            _err(exc)
        return {"input": inp, "diagnostics": diags}

    @app.post("/api/compute")
    def compute(body: ComputeBody):
        return run_compute(store, body.branch_id, body.actor)

    @app.post("/api/decisions", status_code=201)
    def decide(body: DecisionBody):
        try:
            return domain.decide(store, body.nucleus, body.cand_id,
                                 body.action, body.actor, body.note,
                                 body.branch_id)
        except ValueError as exc:
            _err(exc)

    @app.post("/api/resolve", status_code=201)
    def resolve(body: ResolveBody):
        try:
            return domain.resolve_conflict(store, body.nucleus,
                                           body.chosen_decision_id, body.actor)
        except ValueError as exc:
            _err(exc)

    @app.get("/api/replay/{event_id}")
    def replay(event_id: str):
        try:
            return domain.replay(store, event_id)
        except ValueError as exc:
            _err(exc, 404)

    @app.get("/api/export")
    def export():
        return JSONResponse(store.export_data())

    @app.post("/api/import")
    def import_(data: dict):
        try:
            store.import_data(data)
        except ValueError as exc:
            _err(exc, 409)
        return {"imported": True,
                "events": len(store.events_all()),
                "candidates": len(store.candidates_all())}

    @app.get("/api/samples")
    def get_samples():
        return casepack.samples()

    @app.post("/api/casepack/load")
    def load_casepack():
        try:
            log = casepack.run_casepack(store)
        except ValueError as exc:
            _err(exc, 409)
        return {"loaded": True, "log": log}

    return app


app = create_app()
