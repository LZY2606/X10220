from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, JSONResponse

from . import db, scenario, store
from .errors import ConflictError, DomainError, FormatError, NotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASEPACK_ROOT = REPO_ROOT / "casepack"
WEB_INDEX = Path(__file__).resolve().parent / "web" / "index.html"


def create_app(db_path="nmrwb.db", casepack_root=None) -> FastAPI:
    casepack_root = Path(casepack_root or os.environ.get("NMRWB_CASEPACK", DEFAULT_CASEPACK_ROOT))
    app = FastAPI(title="NMR Assignment Workbench", version="1.0.0")
    conn = db.connect(db_path)
    lock = threading.RLock()

    @app.exception_handler(FormatError)
    async def format_handler(_request, exc):
        return JSONResponse(status_code=422, content={"error": "format", "detail": str(exc)})

    @app.exception_handler(DomainError)
    async def domain_handler(_request, exc):
        return JSONResponse(status_code=422, content={"error": "domain", "detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def notfound_handler(_request, exc):
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request, exc):
        return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})

    @app.get("/")
    def index():
        return FileResponse(WEB_INDEX)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "nmr-assignment-workbench"}

    @app.get("/api/casepacks")
    def casepacks():
        result = []
        for manifest_path in sorted(casepack_root.glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result.append({
                "id": manifest["id"],
                "title": manifest["title"],
                "compound": manifest.get("compound"),
                "scenarios": scenario.list_scenarios(manifest_path.parent),
            })
        return result

    @app.get("/api/projects")
    def projects():
        with lock:
            return store.list_projects(conn)

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        name = body.get("name", "未命名化合物分析")
        casepack_id = body.get("casepack")
        with lock:
            project_id = store.create_project(conn, name)
            manifest = None
            if casepack_id:
                manifest = store.load_casepack(conn, project_id, casepack_root / casepack_id)
            conn.commit()
            return {"project_id": project_id, "manifest": manifest}

    @app.get("/api/projects/{project_id}/state")
    def project_state(project_id: str, branch: str | None = None, asof: int | None = None):
        with lock:
            return store.state(conn, project_id, branch_id=branch, asof=asof)

    @app.post("/api/projects/{project_id}/sources")
    def post_source(project_id: str, body: dict = Body(...)):
        with lock:
            result = store.commit_source(
                conn, project_id, body["kind"], body["content"],
                meta=body.get("meta"), branch_id=body.get("branch_id"),
            )
            conn.commit()
            return result

    @app.post("/api/projects/{project_id}/conditions")
    def post_conditions(project_id: str, body: dict = Body(...)):
        with lock:
            result = store.commit_conditions(
                conn, project_id, body["solvent"], str(body["temperature"]),
                body["reference"], str(body["frequency_mhz"]),
                branch_id=body.get("branch_id"),
            )
            conn.commit()
            return result

    @app.post("/api/projects/{project_id}/compute")
    def post_compute(project_id: str, body: dict = Body(default={})):
        with lock:
            result = store.compute(conn, project_id, branch_id=(body or {}).get("branch_id"))
            conn.commit()
            return result

    @app.post("/api/projects/{project_id}/decisions")
    def post_decision(project_id: str, body: dict = Body(...)):
        with lock:
            result = store.decide(
                conn, project_id, body["action"], body["editor"],
                candidate_id=body.get("candidate_id"), note=body.get("note", ""),
                base_branch=body.get("base_branch"), params_patch=body.get("params"),
            )
            conn.commit()
            return result

    @app.post("/api/projects/{project_id}/decisions/{decision_id}/undo")
    def post_undo(project_id: str, decision_id: str):
        with lock:
            result = store.undo_decision(conn, project_id, decision_id)
            conn.commit()
            return result

    @app.post("/api/projects/{project_id}/conflicts/resolve")
    def post_resolve(project_id: str, body: dict = Body(...)):
        with lock:
            result = store.resolve_conflict(
                conn, project_id, body["keep_decision_id"], body["drop_decision_id"]
            )
            conn.commit()
            return result

    @app.post("/api/projects/{project_id}/scenarios/{scenario_id}")
    def post_scenario(project_id: str, scenario_id: str):
        with lock:
            result = scenario.apply_scenario(
                conn, project_id, casepack_root / _casepack_id(conn, project_id), scenario_id
            )
            conn.commit()
            return result

    @app.get("/api/projects/{project_id}/export")
    def export(project_id: str):
        with lock:
            return store.export_project(conn, project_id)

    @app.post("/api/import")
    def import_archive(body: dict = Body(...)):
        with lock:
            project_id = store.import_project(conn, body)
            conn.commit()
            return {"project_id": project_id}

    return app


def _casepack_id(conn, project_id) -> str:
    project = store_row(conn, project_id)
    meta = json.loads(project["meta"] or "{}")
    return meta.get("casepack", "nmr_resonance_atlas")


def store_row(conn, project_id):
    from .db import row
    return row(conn, "SELECT * FROM projects WHERE id=?", (project_id,))
