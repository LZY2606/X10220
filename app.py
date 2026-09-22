"""FastAPI application for the NMR assignment candidate-network workbench.

All state lives in a local SQLite file (default ``./nmrwb_state.db``).  The app
is fully offline: no external services are contacted.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nmrwb import db
from nmrwb.parser import parse_source, FormatError, VALID_KINDS
from nmrwb.service import ProjectService, ServiceError

DB_PATH = os.environ.get("NMRWB_DB", str(pathlib.Path("nmrwb_state.db")))
CASE_DIR = pathlib.Path(__file__).parent / "data" / "case001"

app = FastAPI(title="NMR Resonance Evidence Room", version="1.0.0")
web_dir = pathlib.Path(__file__).parent / "web"

_clock: Optional[db.Clock] = None


def get_clock() -> db.Clock:
    global _clock
    if _clock is None:
        _clock = db.Clock()
    return _clock


def service() -> ProjectService:
    conn = db.connect(DB_PATH)
    return ProjectService(conn, get_clock())


# Allow tests to inject a deterministic virtual clock.
def set_clock(clock: db.Clock) -> None:
    global _clock
    _clock = clock


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ProjectIn(BaseModel):
    project_id: str
    name: str


class SourceIn(BaseModel):
    source_id: str
    kind: str
    filename: str
    text: str
    actor: str = "importer"
    branch_id: str = "B:main"


class RevisionIn(BaseModel):
    text: str
    actor: str
    reason: str = ""
    branch_id: str = "B:main"


class DecisionIn(BaseModel):
    candidate_id: str
    action: str            # accept | reject
    actor: str
    reason: str = ""
    branch_id: str = "B:main"
    expected_base_event_id: Optional[str] = None
    create_child_branch: bool = True


class ResolveIn(BaseModel):
    candidate_id: str
    resolution: str        # accept | reject | keep_both | retract
    actor: str
    reason: str = ""
    branch_id: str


class RetractIn(BaseModel):
    candidate_id: str
    actor: str
    reason: str = ""
    branch_id: str


class TimeIn(BaseModel):
    advance_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/time")
def advance_time(body: TimeIn):
    """Test/demo helper: advance the virtual clock."""
    return {"now": get_clock().advance(body.advance_seconds)}


@app.post("/api/projects")
def create_project(body: ProjectIn):
    svc = service()
    try:
        svc.create_project(body.project_id, body.name)
        svc.conn.commit()
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"project_id": body.project_id, "name": body.name}


@app.get("/api/projects/{pid}")
def get_project(pid: str, branch: str = "B:main",
                at_event: Optional[str] = None):
    svc = service()
    try:
        snap = svc.snapshot(pid, branch, at_event)
    except Exception as exc:
        raise HTTPException(404, str(exc))
    snap["branches"] = svc.branches(pid)
    snap["sources"] = [
        {"source_id": s["source_id"], "version_id": s["version_id"],
         "parent_version_id": s["parent_version_id"], "kind": s["kind"],
         "filename": s["filename"], "content_hash": s["content_hash"],
         "active": snap["active_versions"].get(s["source_id"]) == s["version_id"]}
        for s in svc.sources(pid)]
    return snap


@app.get("/api/projects/{pid}/events")
def get_events(pid: str):
    return service().events(pid)


@app.get("/api/projects/{pid}/runs")
def get_runs(pid: str):
    return service().runs(pid)


@app.get("/api/projects/{pid}/sources/{sid}/{vid}")
def get_source_text(pid: str, sid: str, vid: str):
    row = db.get_source(service().conn, pid, sid, vid)
    if row is None:
        raise HTTPException(404, "source version not found")
    return {"source_id": sid, "version_id": vid, "kind": row["kind"],
            "filename": row["filename"], "raw_text": row["raw_text"],
            "content_hash": row["content_hash"],
            "parent_version_id": row["parent_version_id"]}


@app.post("/api/projects/{pid}/sources")
def submit_source(pid: str, body: SourceIn):
    if body.kind not in VALID_KINDS:
        raise HTTPException(400, f"invalid kind {body.kind!r}")
    svc = service()
    try:
        out = svc.submit_source(pid, body.source_id, body.kind, body.filename,
                                body.text, body.actor, body.branch_id)
    except FormatError as exc:
        raise HTTPException(422, {"error": "format", "message": str(exc),
                                  "line": getattr(exc, "line", 0)})
    except ServiceError as exc:
        raise HTTPException(409, str(exc))
    return {"event_id": out["event"]["event_id"],
            "diagnostics": out["parsed"]["diagnostics"],
            "run_id": out["run"]["run_id"] if out["run"] else None,
            "statistics": out["run"]["statistics"] if out["run"] else None}


@app.post("/api/projects/{pid}/sources/{sid}/revisions")
def revise_source(pid: str, sid: str, body: RevisionIn):
    svc = service()
    try:
        out = svc.revise_source(pid, sid, body.text, body.actor,
                                body.reason, body.branch_id)
    except FormatError as exc:
        raise HTTPException(422, {"error": "format", "message": str(exc),
                                  "line": getattr(exc, "line", 0)})
    except ServiceError as exc:
        raise HTTPException(404, str(exc))
    return {"event_id": out["event"]["event_id"],
            "changed_records": out["event"]["payload"].get("changed_records"),
            "run_id": out["run"]["run_id"] if out["run"] else None,
            "statistics": out["run"]["statistics"] if out["run"] else None}


@app.post("/api/projects/{pid}/decisions")
def make_decision(pid: str, body: DecisionIn):
    if body.action not in ("accept", "reject"):
        raise HTTPException(400, "action must be accept or reject")
    svc = service()
    try:
        out = svc.decide(pid, body.branch_id, body.candidate_id, body.action,
                         body.actor, body.reason, body.expected_base_event_id,
                         body.create_child_branch)
    except ServiceError as exc:
        raise HTTPException(404, str(exc))
    return {"event_id": out["event"]["event_id"],
            "branch_id": out["branch_id"],
            "propagation": out["propagation"],
            "conflict_event_id": out["conflict_event"]["event_id"]
            if out["conflict_event"] else None}


@app.post("/api/projects/{pid}/conflicts/resolve")
def resolve_conflict(pid: str, body: ResolveIn):
    svc = service()
    try:
        ev = svc.resolve_conflict(pid, body.branch_id, body.candidate_id,
                                  body.resolution, body.actor, body.reason)
    except ServiceError as exc:
        raise HTTPException(400, str(exc))
    return ev


@app.post("/api/projects/{pid}/decisions/retract")
def retract_decision(pid: str, body: RetractIn):
    return service().retract_decision(pid, body.branch_id, body.candidate_id,
                                      body.actor, body.reason)


@app.get("/api/projects/{pid}/export")
def export_project(pid: str):
    bundle = service().export_bundle(pid)
    return JSONResponse(bundle)


@app.post("/api/import-bundle")
async def import_bundle(request: Request, new_project_id: Optional[str] = None,
                        fresh_database: bool = False):
    """Import an uploaded bundle.

    ``fresh_database=true`` wipes the local sqlite file first, reproducing the
    "delete database then import" acceptance path.  ``new_project_id`` may
    remap the project to avoid collisions.
    """
    bundle = await request.json()
    if fresh_database:
        try:
            os.remove(DB_PATH)
        except FileNotFoundError:
            pass
    if new_project_id:
        bundle = dict(bundle)
        bundle["project"] = dict(bundle["project"])
        bundle["project"]["project_id"] = new_project_id
        for table in ("branches", "sources", "runs"):
            for row in bundle.get(table, []):
                row["project_id"] = new_project_id
        for ev in bundle.get("events", []):
            ev["project_id"] = new_project_id
    svc = service()
    try:
        pid = db.import_bundle(svc.conn, bundle)
        svc.conn.commit()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"project_id": pid, "events": len(bundle.get("events", []))}


@app.post("/api/projects/{pid}/load-case")
def load_case_pack(pid: str, actor: str = "importer"):
    """Load the repository-shipped offline case pack into a project."""
    svc = service()
    files = {
        "S1H": ("1h", "1h_ethylbenzene.txt"),
        "S13C": ("13c", "13c_ethylbenzene.txt"),
        "COSY": ("cosy", "cosy.txt"),
        "HSQC": ("hsqc", "hsqc.txt"),
        "HMBC": ("hmbc", "hmbc.txt"),
        "SKEL": ("skeleton", "skeleton_ethylbenzene.txt"),
        "RULES": ("rules", "rules_v1.txt"),
    }
    events = []
    for sid, (kind, fn) in files.items():
        text = (CASE_DIR / fn).read_text()
        out = svc.submit_source(pid, sid, kind, fn, text, actor)
        events.append(out["event"]["event_id"])
    return {"loaded": list(files), "events": events}


@app.get("/api/archive")
def get_archive():
    path = pathlib.Path("nmrbench/generated/archive.json")
    if not path.exists():
        raise HTTPException(404, "archive not built; run "
                                "python -m nmrbench.run_archive")
    return JSONResponse(json.loads(path.read_text()))


@app.get("/", response_class=HTMLResponse)
def index():
    return (web_dir / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@app.get("/api/projects/{pid}/view")
def project_view(pid: str, branch: str = "B:main",
                 at_event: Optional[str] = None):
    """Aggregated view for the linked review UI: parsed peaks/edges, skeleton
    shapes and the current run.  Every value carries stable ids/provenance."""
    svc = service()
    snap = svc.snapshot(pid, branch, at_event)
    parsed: Dict[str, dict] = {}
    for sid, vid in snap["active_versions"].items():
        row = db.get_source(svc.conn, pid, sid, vid)
        if row is None:
            continue
        ps = parse_source(row["raw_text"], sid, vid)
        parsed[sid] = {"kind": ps.kind, "ref": f"{sid}/{vid}",
                       "meta": ps.meta, "data": ps.to_dict(),
                       "diagnostics": [d.to_dict() for d in ps.diagnostics]}
    skeleton = None
    for sid, d in parsed.items():
        if d["kind"] == "skeleton":
            from nmrwb.layout import skeleton_shapes
            skeleton = {"ref": d["ref"],
                        "shapes": skeleton_shapes(
                            parse_source(
                                db.get_source(svc.conn, pid, sid,
                                              snap["active_versions"][sid])
                                ["raw_text"], sid,
                                snap["active_versions"][sid]))}
    decisions_by_candidate: Dict[str, List[dict]] = {}
    for d in snap["decisions"]:
        cid = d.get("candidate_id")
        if cid:
            decisions_by_candidate.setdefault(cid, []).append(d)
    return {
        "project_id": pid,
        "branch_id": branch,
        "at_event": at_event,
        "active_versions": snap["active_versions"],
        "parsed": parsed,
        "skeleton": skeleton,
        "run": snap["current_run"],
        "decisions_by_candidate": decisions_by_candidate,
        "events": snap["events"],
        "branches": svc.branches(pid),
    }
