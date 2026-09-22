from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .casepack import PACK_DIR, run_narrative
from .domain import IngestError
from .engine import Engine
from .store import State, Store

PAGE = open(os.path.join(os.path.dirname(__file__), "page.html"), encoding="utf-8").read()


class DocumentIn(BaseModel):
    stream: str
    kind: str
    name: str = ""
    format: str = "csv"
    raw: str
    actor: str = "reviewer"


class DecisionIn(BaseModel):
    candidate_id: str
    verdict: str
    actor: str = "reviewer"
    note: str = ""
    base_branch: Optional[str] = None


class ActorIn(BaseModel):
    actor: str = "reviewer"


class ResolveIn(BaseModel):
    resolution: str
    actor: str = "reviewer"
    note: str = ""


def _shape(state: State, as_of_seq: int, branch: str, store: Store) -> dict:
    current_docs = []
    for stream, doc_id in sorted(state.current.items()):
        d = state.docs[doc_id]
        current_docs.append({
            "doc_id": d["doc_id"], "stream": stream, "kind": d["kind"],
            "name": d["name"], "version": d["version"], "parent": d["parent"],
            "format": d["format"], "n_diagnostics": len(d["diagnostics"]),
        })
    versions = {s: [ {"doc_id": d, "version": state.docs[d]["version"]}
                     for d in v] for s, v in state.doc_versions.items()}
    peaks, edges, conditions, params = [], [], {}, {}
    for doc_id in state.current.values():
        d = state.docs[doc_id]
        if d["kind"] == "peaks":
            for pk in d["parsed"]["peaks"]:
                peaks.append(dict(pk, doc_id=doc_id, version=d["version"]))
        elif d["kind"] == "edges":
            for e in d["parsed"]["edges"]:
                edges.append(dict(e, doc_id=doc_id, version=d["version"]))
        elif d["kind"] == "conditions":
            conditions = d["parsed"]["conditions"]
        elif d["kind"] == "params":
            params = d["parsed"]
    candidates = sorted(state.candidates.values(),
                        key=lambda c: (c["site"], -c["score"], c["peak"]))
    latest_run = state.runs[max(state.runs, key=lambda r: state.runs[r]["cause_seq"])] \
        if state.runs else None
    events = [{"seq": e["seq"], "id": e["id"], "branch": e["branch"],
               "type": e["type"], "actor": e["actor"], "at": e["at"]}
              for e in store.events()]
    return {
        "meta": {"as_of_seq": as_of_seq, "branch": branch,
                 "current_branch": store.current_branch(),
                 "is_history": as_of_seq < (events[-1]["seq"] if events else 0)
                               or branch != store.current_branch()},
        "documents": current_docs, "versions": versions,
        "peaks": peaks, "edges": edges, "conditions": conditions, "params": params,
        "candidates": candidates,
        "site_status": latest_run["site_status"] if latest_run else [],
        "decisions": sorted(state.decisions.values(), key=lambda d: d["seq"]),
        "conflicts": list(state.conflicts.values()),
        "judgments": state.judgments,
        "diagnostics": state.diagnostics,
        "branches": state.branches,
        "events": events,
    }


def _resolve_as_of(store: Store, as_of: Optional[str]) -> int:
    events = store.events()
    if not events:
        return 0
    if not as_of:
        return events[-1]["seq"]
    if as_of.isdigit():
        return int(as_of)
    for e in events:
        if e["id"] == as_of:
            return e["seq"]
    raise HTTPException(404, "unknown event %s" % as_of)


def _molecule_svg(state: State) -> str:
    doc = next((state.docs[d] for d in state.current.values()
                if state.docs[d]["kind"] == "molecule"), None)
    if doc is None:
        return "<svg xmlns='http://www.w3.org/2000/svg' width='420' height='360'></svg>"
    mol = doc["parsed"]
    decided_sites = {c["site"] for c in state.candidates.values()
                     if c["status"] == "current" and c.get("decision")
                     and c["decision"]["verdict"] == "accept"}
    current_sites = {c["site"] for c in state.candidates.values()
                     if c["status"] == "current"}
    stale_sites = {c["site"] for c in state.candidates.values()
                   if c["status"] == "stale"}
    pos = {s["site"]: (110 + s["x"] * 62, 190 - s["y"] * 62) for s in mol["sites"]}
    parts = ["<svg xmlns='http://www.w3.org/2000/svg' width='420' height='380' "
             "viewBox='0 0 420 380' style='background:#fafaf7;border:1px solid #ddd'>"]
    for a, b in mol["bonds"]:
        x1, y1 = pos[a]
        x2, y2 = pos[b]
        parts.append("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' stroke='#999' "
                     "stroke-width='1.5'/>" % (x1, y1, x2, y2))
    for s in mol["sites"]:
        x, y = pos[s["site"]]
        if s["site"] in decided_sites:
            color = "#2e9e5b"
        elif s["site"] in current_sites:
            color = "#2b6cb0"
        elif s["site"] in stale_sites:
            color = "#d97706"
        else:
            color = "#9ca3af"
        parts.append(
            "<g class='site' data-site='%s' style='cursor:pointer'>"
            "<circle cx='%.1f' cy='%.1f' r='13' fill='%s' fill-opacity='0.25' "
            "stroke='%s' stroke-width='2'/>"
            "<text x='%.1f' y='%.1f' font-size='10' text-anchor='middle' "
            "dominant-baseline='central' fill='#222'>%s</text></g>"
            % (s["site"], x, y, color, color, x, y, s["site"]))
    parts.append("</svg>")
    return "".join(parts)


def create_app(db_path: str = "workbench.db", clock=None) -> FastAPI:
    store = Store(db_path, clock=clock)
    engine = Engine(store)
    app = FastAPI(title="NMR 共振经纬 — 归属证据室")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/api/state")
    def get_state(as_of: Optional[str] = Query(None),
                  branch: Optional[str] = Query(None)):
        seq = _resolve_as_of(store, as_of)
        state = store.replay(as_of_seq=seq, branch=branch)
        return _shape(state, seq, branch or store.current_branch(), store)

    @app.post("/api/documents")
    def submit(doc: DocumentIn):
        try:
            return engine.submit_document(doc.stream, doc.kind, doc.name,
                                          doc.format, doc.raw, actor=doc.actor)
        except IngestError as e:
            raise HTTPException(422, {"errors": e.messages})

    @app.post("/api/decisions")
    def decide(inp: DecisionIn):
        try:
            return engine.decide(inp.candidate_id, inp.verdict, inp.actor,
                                 inp.note, inp.base_branch)
        except IngestError as e:
            raise HTTPException(422, {"errors": e.messages})

    @app.post("/api/decisions/{decision_id}/revert")
    def revert(decision_id: str, inp: ActorIn):
        try:
            return engine.revert(decision_id, inp.actor)
        except IngestError as e:
            raise HTTPException(422, {"errors": e.messages})

    @app.post("/api/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: str, inp: ResolveIn):
        try:
            return engine.resolve_conflict(conflict_id, inp.resolution,
                                           inp.actor, inp.note)
        except IngestError as e:
            raise HTTPException(422, {"errors": e.messages})

    @app.get("/api/export")
    def export():
        return store.export_archive()

    @app.post("/api/import")
    def import_(archive: dict):
        try:
            return store.import_archive(archive)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/reset")
    def reset():
        store.reset()
        return {"ok": True}

    @app.post("/api/casepack/run")
    def casepack_run():
        return run_narrative(engine, PACK_DIR)

    @app.get("/api/sample/{name}")
    def sample(name: str):
        allowed = {n for _s, _k, n, _f in
                   [("molecule", "molecule", "molecule.json", "json"),
                    ("peaks_1h", "peaks", "peaks_1h.csv", "csv"),
                    ("peaks_13c", "peaks", "peaks_13c.csv", "csv"),
                    ("cosy", "edges", "cosy.csv", "csv"),
                    ("hsqc", "edges", "hsqc.csv", "csv"),
                    ("hmbc", "edges", "hmbc.csv", "csv"),
                    ("conditions", "conditions", "conditions.json", "json"),
                    ("params", "params", "params.json", "json")]}
        if name not in allowed:
            raise HTTPException(404, "unknown sample")
        with open(os.path.join(PACK_DIR, name), encoding="utf-8") as fh:
            return {"name": name, "raw": fh.read()}

    @app.get("/api/molecule.svg")
    def molecule_svg(as_of: Optional[str] = Query(None),
                     branch: Optional[str] = Query(None)):
        seq = _resolve_as_of(store, as_of)
        state = store.replay(as_of_seq=seq, branch=branch)
        return Response(content=_molecule_svg(state), media_type="image/svg+xml")

    return app
