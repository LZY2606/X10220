"""FastAPI surface for the NMR assignment workbench."""
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import casepack, importer, projection, scenarios, service
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class ImportBody(BaseModel):
    kind: str
    text: str
    source: str = "manual"
    actor: str = "reviewer"


class ComputeBody(BaseModel):
    params: dict = None
    actor: str = "reviewer"


class DecideBody(BaseModel):
    candidate_id: str
    verdict: str
    actor: str = "reviewer"


class ResolveBody(BaseModel):
    candidate_id: str
    chosen_verdict: str
    note: str = ""
    actor: str = "reviewer"


class UndoBody(BaseModel):
    target_seq: int
    actor: str = "reviewer"


class CheckoutBody(BaseModel):
    seq: int


class ContextBody(BaseModel):
    context: dict
    affected_nuclei: list
    reason: str = ""
    actor: str = "reviewer"


def create_app(db_path=":memory:"):
    store = Store(db_path)
    app = FastAPI(title="NMR 共振经纬：化学位移、耦合候选与归属证据室")
    app.state.store = store

    def _project_or_404(project_id):
        if store.get_project(project_id) is None:
            raise HTTPException(404, "unknown project %s" % project_id)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/casepack")
    def get_casepack():
        return {"compound": casepack.load_compound(),
                "scenarios": casepack.list_scenarios()}

    @app.post("/api/projects/load_casepack")
    def load_casepack_project():
        compound = casepack.load_compound()
        project_id = "proj-resonance-atlas"
        suffix = 0
        base = project_id
        while store.get_project(project_id) is not None:
            suffix += 1
            project_id = "%s-%d" % (base, suffix)
        store.create_project(project_id, "NMR 共振经纬：化学位移、耦合候选与归属证据室")
        for kind, text in casepack.baseline_files().items():
            service.import_text(store, project_id, kind, text,
                                source="casepack/baseline", actor="curator")
        res = service.run_compute(store, project_id, compound, actor="curator")
        return {"project_id": project_id, "n_candidates": len(res["candidates"]),
                "fingerprint": res["fingerprint"]}

    @app.get("/api/projects")
    def list_projects():
        return {"projects": store.list_projects()}

    @app.get("/api/projects/{project_id}/state")
    def get_state(project_id: str, at_seq: int = None):
        _project_or_404(project_id)
        state = projection.replay(store, project_id, at_seq=at_seq)
        state["branches"] = store.branches_of(project_id)
        latest = {}
        for rec in store.inputs_of(project_id):
            latest[rec["kind"]] = rec
        state["input_records"] = list(latest.values())
        state["compound"] = casepack.load_compound()
        state["dangling_edges"] = scenarios._dangling_edges(store, project_id)
        state["scenarios"] = casepack.list_scenarios()
        return JSONResponse(json.loads(json.dumps(state, ensure_ascii=False)))

    @app.post("/api/projects/{project_id}/import")
    def import_input(project_id: str, body: ImportBody):
        _project_or_404(project_id)
        if body.kind not in service.INPUT_KINDS:
            raise HTTPException(422, "kind must be one of %s" % (service.INPUT_KINDS,))
        try:
            rec = service.import_text(store, project_id, body.kind, body.text,
                                      source=body.source, actor=body.actor)
        except importer.ImportError_ as exc:
            return JSONResponse({"errors": exc.errors}, status_code=422)
        return {"input_id": rec["id"], "version": rec["version"],
                "parent_id": rec["parent_id"],
                "diagnostics": rec["content"].get("diagnostics", [])}

    @app.post("/api/projects/{project_id}/compute")
    def compute(project_id: str, body: ComputeBody = None):
        _project_or_404(project_id)
        body = body or ComputeBody()
        res = service.run_compute(store, project_id, casepack.load_compound(),
                                  params=body.params, actor=body.actor)
        return {"fingerprint": res["fingerprint"], "event_seq": res["event"]["seq"],
                "n_candidates": len(res["candidates"]),
                "overlap_groups": res["overlap_groups"]}

    @app.post("/api/projects/{project_id}/decide")
    def decide(project_id: str, body: DecideBody):
        _project_or_404(project_id)
        try:
            res = service.decide(store, project_id, body.candidate_id,
                                 body.verdict, actor=body.actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"event_seq": res["event"]["seq"], "impact": res["impact"]}

    @app.post("/api/projects/{project_id}/resolve")
    def resolve(project_id: str, body: ResolveBody):
        _project_or_404(project_id)
        res = service.resolve(store, project_id, body.candidate_id,
                              body.chosen_verdict, actor=body.actor, note=body.note)
        return {"event_seq": res["event"]["seq"]}

    @app.post("/api/projects/{project_id}/undo")
    def undo(project_id: str, body: UndoBody):
        _project_or_404(project_id)
        try:
            res = service.undo(store, project_id, body.target_seq, actor=body.actor)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return {"event_seq": res["event"]["seq"]}

    @app.post("/api/projects/{project_id}/context")
    def context(project_id: str, body: ContextBody):
        _project_or_404(project_id)
        res = service.change_context(store, project_id, body.context,
                                     body.affected_nuclei, body.reason, actor=body.actor)
        return {"event_seq": res["event"]["seq"]}

    @app.post("/api/projects/{project_id}/checkout")
    def checkout(project_id: str, body: CheckoutBody):
        _project_or_404(project_id)
        try:
            store.checkout(project_id, body.seq)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return {"active_tip": body.seq}

    @app.get("/api/projects/{project_id}/events")
    def events(project_id: str):
        _project_or_404(project_id)
        proj = store.get_project(project_id)
        return {"events": store.chain_to(proj["active_tip"]),
                "branches": store.branches_of(project_id)}

    @app.post("/api/projects/{project_id}/scenario/{name}")
    def run_scenario(project_id: str, name: str):
        _project_or_404(project_id)
        try:
            return scenarios.run_scenario(store, project_id,
                                          casepack.load_compound(), name)
        except KeyError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/projects/{project_id}/export")
    def export(project_id: str):
        _project_or_404(project_id)
        return JSONResponse(json.loads(
            json.dumps(store.export_project(project_id), ensure_ascii=False)))

    @app.post("/api/import_bundle")
    def import_bundle(bundle: dict):
        try:
            project_id = store.import_project(bundle)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"project_id": project_id}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
