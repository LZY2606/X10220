"""Project service: commands append events; history is never overwritten.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import repository as repo
from . import store
from .clock import iso_at
from .dsl import DSLError, parse_document
from .engine import DEFAULT_PARAMS, generate, params_fingerprint
from .ids import digest, id_of
from .replay import MAIN_BRANCH, Snapshot

ROLE_FOR_KIND = {
    "mol": "mol",
    "peaks": "peaks",
    "edges": "edges",
    "conditions": "conditions",
}


class ConflictError(ValueError):
    """Raised when a decision would conflict and force=False."""


def create_project(conn: sqlite3.Connection, name: str = "case") -> Dict[str, Any]:
    project_id = id_of("proj", {"name": name, "seed": "20260923"})
    store.set_meta(conn, "project_id", project_id)
    store.set_meta(conn, "name", name)
    store.set_meta(conn, "tick", 0)
    next_seq = 1
    event_id = id_of("evt", {"seq": next_seq, "kind": "project_created"})
    repo.append_event(
        conn,
        event_id,
        next_seq,
        MAIN_BRANCH,
        "project_created",
        "system",
        0,
        {"project_id": project_id, "name": name},
        [],
    )
    repo.upsert_branch(conn, MAIN_BRANCH, "main", None, next_seq, event_id)
    conn.commit()
    return {"project_id": project_id, "event_id": event_id}


def _tick(conn) -> Tuple[int, int, int]:
    seq = store.next_seq(conn)
    tick = store.next_tick(conn)
    store.set_meta(conn, "tick", tick)
    return seq, tick


def snapshot(conn, branch_id: str = MAIN_BRANCH, at_seq: Optional[int] = None):
    return Snapshot(conn, branch_id, at_seq)


def import_source(
    conn,
    kind: str,
    filename: str,
    content: str,
    actor: str = "reviewer",
    branch_id: str = MAIN_BRANCH,
) -> Dict[str, Any]:
    parsed = parse_document(kind, content)
    role = ROLE_FOR_KIND[kind]
    existing = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM sources WHERE role=?",
        (role,),
    ).fetchone()
    version = int(existing["v"]) + 1
    parent = conn.execute(
        "SELECT source_id FROM sources WHERE role=? ORDER BY version DESC LIMIT 1",
        (role,),
    ).fetchone()
    seq, tick = _tick(conn)
    ordinal = seq
    provisional_event = id_of("evt", {"seq": seq, "kind": "source_imported",
                                      "role": role, "version": version})
    source_id = id_of(
        "src",
        {
            "role": role,
            "version": version,
            "content": content,
            "filename": filename,
        },
    )
    source = {
        "source_id": source_id,
        "role": role,
        "kind": kind,
        "filename": filename,
        "version": version,
        "parent_source_id": parent["source_id"] if parent else None,
        "content": content,
        "import_event_id": provisional_event,
        "ordinal": ordinal,
    }
    repo.insert_source(conn, source, parsed)
    diagnostics = list(parsed.get("diagnostics", []))
    if kind == "peaks":
        _insert_peaks(conn, parsed, source_id)
    elif kind == "edges":
        diagnostics.extend(_insert_edges(conn, parsed, source_id))
    elif kind == "mol":
        _insert_mol(conn, parsed, source_id)
    payload = {
        "source_id": source_id,
        "role": role,
        "kind": kind,
        "filename": filename,
        "version": version,
        "parent_source_id": source["parent_source_id"],
        "content_sha256": digest(content),
        "diagnostics": diagnostics,
        "n_records": len(parsed.get("rows", []))
        if "rows" in parsed
        else len(parsed.get("sites", []))
        + len(parsed.get("groups", []))
        + len(parsed.get("expected_edges", [])),
    }
    parents = _branch_parent_events(conn, branch_id)
    repo.append_event(
        conn,
        provisional_event,
        seq,
        branch_id,
        "source_imported",
        actor,
        tick,
        payload,
        parents,
    )
    _advance_branch(conn, branch_id, seq, provisional_event)
    conn.commit()
    return {
        "source_id": source_id,
        "event_id": provisional_event,
        "version": version,
        "diagnostics": diagnostics,
    }


def _branch_parent_events(conn, branch_id: str) -> List[str]:
    row = conn.execute(
        "SELECT head_seq FROM branches WHERE branch_id=?", (branch_id,)
    ).fetchone()
    if row is None or row["head_seq"] == 0:
        return []
    head = conn.execute(
        "SELECT event_id FROM events WHERE seq=?", (row["head_seq"],)
    ).fetchone()
    return [head["event_id"]] if head else []


def _advance_branch(conn, branch_id: str, seq: int, event_id: str) -> None:
    conn.execute(
        "UPDATE branches SET head_seq=? WHERE branch_id=?", (seq, branch_id)
    )


def _insert_peaks(conn, parsed: Dict[str, Any], source_id: str) -> None:
    for index, row in enumerate(parsed["rows"]):
        peak_id = id_of("pk", [source_id, row["local_id"]])
        repo.insert_peak(
            conn,
            {
                "peak_id": peak_id,
                "source_id": source_id,
                "local_id": row["local_id"],
                "nucleus": row["nucleus"],
                "shift_ppm": row["shift_ppm"],
                "shift_raw": row["shift_raw"],
                "shift_unit": row.get("shift_unit", "ppm"),
                "intensity": row.get("intensity"),
                "intensity_raw": row["intensity_raw"],
                "intensity_unit": row.get("intensity_unit"),
                "integral": row.get("integral"),
                "integral_raw": row["integral_raw"],
                "integral_unit": row.get("integral_unit"),
                "multiplicity": row["multiplicity"],
                "tags": row.get("tags", []),
                "tags_raw": row.get("tags_raw", ""),
                "ordinal": index,
            },
        )


def _all_peaks(conn):
    return {p["local_id"]: dict(p) for p in conn.execute(
        "SELECT * FROM peaks ORDER BY ordinal").fetchall()}


def _insert_edges(conn, parsed: Dict[str, Any], source_id: str):
    diagnostics = []
    peaks = _all_peaks(conn)
    for index, row in enumerate(parsed["rows"]):
        from_peak = peaks.get(row["from_peak_ref"])
        to_peak = peaks.get(row["to_peak_ref"])
        status = "resolved" if (from_peak and to_peak) else "unresolved_reference"
        edge_id = id_of("ed", [source_id, row["local_id"]])
        repo.insert_edge(
            conn,
            {
                "edge_id": edge_id,
                "source_id": source_id,
                "local_id": row["local_id"],
                "edge_kind": row["edge_kind"],
                "from_peak_id": from_peak["peak_id"] if from_peak else None,
                "to_peak_id": to_peak["peak_id"] if to_peak else None,
                "from_ref": row["from_peak_ref"],
                "to_ref": row["to_peak_ref"],
                "from_ppm": from_peak["shift_ppm"] if from_peak else None,
                "to_ppm": to_peak["shift_ppm"] if to_peak else None,
                "strength": row["strength"],
                "strength_raw": row["strength_raw"],
                "strength_unit": row.get("strength_unit"),
                "status": status,
                "ordinal": index,
            },
        )
        if status != "resolved":
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "unresolved_edge_reference",
                    "message": "edge %s references unknown peak(s) %s/%s; "
                    "edge retained but not used as evidence"
                    % (
                        row["local_id"],
                        row["from_peak_ref"],
                        row["to_peak_ref"],
                    ),
                    "ref": row["local_id"],
                }
            )
    return diagnostics


def _insert_mol(conn, parsed: Dict[str, Any], source_id: str) -> None:
    for index, row in enumerate(parsed["sites"]):
        site_id = id_of("st", [source_id, row["local_id"]])
        repo.insert_site(
            conn,
            {
                "site_id": site_id,
                "source_id": source_id,
                "local_id": row["local_id"],
                "nucleus": row["nucleus"],
                "element": row["element"],
                "predicted_ppm": row["predicted_ppm"],
                "predicted_raw": row["predicted_raw"],
                "shift_unit": row.get("shift_unit", "ppm"),
                "label": row["label"],
                "bound_carbon": row.get("bound_carbon"),
                "ordinal": index,
            },
        )
    for index, row in enumerate(parsed["groups"]):
        group_id = id_of("gr", [source_id, row["local_id"]])
        member_ids = [
            id_of("st", [source_id, local]) for local in row["member_site_ids"]
        ]
        repo.insert_group(
            conn,
            {
                "group_id": group_id,
                "source_id": source_id,
                "local_id": row["local_id"],
                "ordinal": index,
            },
            member_ids,
        )
    for index, row in enumerate(parsed["expected_edges"]):
        expected_id = id_of("ex", [source_id, row["local_id"]])
        repo.insert_expected_edge(
            conn,
            {
                "expected_id": expected_id,
                "source_id": source_id,
                "local_id": row["local_id"],
                "site_a": row["site_a"],
                "site_b": row["site_b"],
                "edge_kind": row["edge_kind"],
                "note": row.get("note", ""),
                "ordinal": index,
            },
        )
