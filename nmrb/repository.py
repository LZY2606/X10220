"""Read/write helpers over the immutable fact tables and the event log."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .clock import iso_at
from .ids import digest, id_of


def _loads(value: str) -> Any:
    return json.loads(value)


# ---------------------------------------------------------------- facts

def insert_source(conn, source: Dict[str, Any], parsed: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO sources(source_id, role, kind, filename, version,
           parent_source_id, content, content_sha256, parsed_json,
           import_event_id, ordinal)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source["source_id"],
            source["role"],
            source["kind"],
            source["filename"],
            source["version"],
            source.get("parent_source_id"),
            source["content"],
            digest(source["content"]),
            json.dumps(parsed, ensure_ascii=False, sort_keys=True),
            source["import_event_id"],
            source["ordinal"],
        ),
    )


def insert_peak(conn, peak: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO peaks(peak_id, source_id, local_id, nucleus,
           shift_ppm, shift_raw, shift_unit, intensity, intensity_raw,
           intensity_unit, integral, integral_raw, integral_unit,
           multiplicity, tags_json, tags_raw, ordinal)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            peak["peak_id"], peak["source_id"], peak["local_id"],
            peak["nucleus"], peak.get("shift_ppm"), peak["shift_raw"],
            peak.get("shift_unit", "ppm"), peak.get("intensity"),
            peak["intensity_raw"], peak.get("intensity_unit"),
            peak.get("integral"), peak["integral_raw"],
            peak.get("integral_unit"), peak["multiplicity"],
            json.dumps(peak.get("tags", []), ensure_ascii=False),
            peak.get("tags_raw", ""), peak["ordinal"],
        ),
    )


def insert_edge(conn, edge: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO edges(edge_id, source_id, local_id, edge_kind,
           from_peak_id, to_peak_id, from_ref, to_ref, from_ppm, to_ppm,
           strength, strength_raw, strength_unit, status, ordinal)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            edge["edge_id"], edge["source_id"], edge["local_id"],
            edge["edge_kind"], edge.get("from_peak_id"), edge.get("to_peak_id"),
            edge["from_ref"], edge["to_ref"], edge.get("from_ppm"),
            edge.get("to_ppm"), edge.get("strength"), edge["strength_raw"],
            edge.get("strength_unit"), edge["status"], edge["ordinal"],
        ),
    )


def insert_site(conn, site: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO sites(site_id, source_id, local_id, nucleus, element,
           predicted_ppm, predicted_raw, shift_unit, label, bound_carbon, ordinal)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            site["site_id"], site["source_id"], site["local_id"],
            site["nucleus"], site["element"], site["predicted_ppm"],
            site["predicted_raw"], site.get("shift_unit", "ppm"),
            site["label"], site.get("bound_carbon"), site["ordinal"],
        ),
    )


def insert_group(conn, group: Dict[str, Any], member_site_ids: List[str]) -> None:
    conn.execute(
        "INSERT INTO groups(group_id, source_id, local_id, ordinal) VALUES(?,?,?,?)",
        (group["group_id"], group["source_id"], group["local_id"], group["ordinal"]),
    )
    for index, site_id in enumerate(member_site_ids):
        conn.execute(
            "INSERT INTO group_members(group_id, site_id, ordinal) VALUES(?,?,?)",
            (group["group_id"], site_id, index),
        )


def insert_expected_edge(conn, edge: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO expected_edges(expected_id, source_id, local_id,
           site_a, site_b, edge_kind, note, ordinal)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            edge["expected_id"], edge["source_id"], edge["local_id"],
            edge["site_a"], edge["site_b"], edge["edge_kind"], edge["note"],
            edge["ordinal"],
        ),
    )


def insert_generation(conn, gen: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO generations(generation_id, algorithm, params_json,
           params_fp, scope_json, direct_json, created_event, superseded_gen,
           ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            gen["generation_id"], gen["algorithm"],
            json.dumps(gen["params"], ensure_ascii=False, sort_keys=True),
            gen["params_fp"],
            json.dumps(gen["scope"], ensure_ascii=False, sort_keys=True),
            json.dumps(gen["direct"], ensure_ascii=False, sort_keys=True),
            gen["created_event"], gen.get("superseded_gen"), gen["ordinal"],
        ),
    )


def insert_candidate(conn, cand: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO candidates(candidate_id, generation_id, target_key,
           target_nucleus, target_label, peak_id, shift_ppm, predicted_ppm,
           delta_ppm, shift_score, edge_score, total_score, rank, status,
           multiplicity, equivalent_n, overlap_group, evidence_json,
           reject_json, notes_json, ordinal)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            cand["candidate_id"], cand["generation_id"], cand["target_key"],
            cand["target_nucleus"], cand["target_label"], cand.get("peak_id"),
            cand.get("shift_ppm"), cand["predicted_ppm"], cand.get("delta_ppm"),
            cand.get("shift_score"), cand.get("edge_score"), cand["total_score"],
            cand["rank"], cand["status"], cand.get("multiplicity", ""),
            cand.get("equivalent_n", 1), cand.get("overlap_group"),
            json.dumps(cand.get("evidence", []), ensure_ascii=False),
            json.dumps(cand.get("reject", []), ensure_ascii=False),
            json.dumps(cand.get("notes", []), ensure_ascii=False),
            cand["ordinal"],
        ),
    )


# ------------------------------------------------------------- event log

def append_event(
    conn,
    event_id: str,
    seq: int,
    branch_id: str,
    kind: str,
    actor: str,
    tick: int,
    payload: Dict[str, Any],
    parents: List[str],
) -> None:
    conn.execute(
        """INSERT INTO events(event_id, seq, branch_id, kind, actor, tick,
           at_iso, payload_json, parents_json, ordinal)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, seq, branch_id, kind, actor, tick, iso_at(tick),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            json.dumps(parents, ensure_ascii=False),
            seq,
        ),
    )


def upsert_branch(
    conn,
    branch_id: str,
    name: str,
    parent_branch: Optional[str],
    head_seq: int,
    created_event: str,
) -> None:
    conn.execute(
        """INSERT INTO branches(branch_id, name, parent_branch, head_seq,
           created_event) VALUES(?,?,?,?,?)
           ON CONFLICT(branch_id) DO UPDATE SET head_seq=excluded.head_seq""",
        (branch_id, name, parent_branch, head_seq, created_event),
    )


def all_events(conn) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    return [_event_row(r) for r in rows]


def _event_row(row) -> Dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "seq": row["seq"],
        "branch_id": row["branch_id"],
        "kind": row["kind"],
        "actor": row["actor"],
        "tick": row["tick"],
        "at_iso": row["at_iso"],
        "payload": _loads(row["payload_json"]),
        "parents": _loads(row["parents_json"]),
    }


def event_by_id(conn, event_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM events WHERE event_id=?", (event_id,)
    ).fetchone()
    return _event_row(row) if row else None


def list_branches(conn) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM branches ORDER BY rowid").fetchall()
    return [dict(r) for r in rows]


def source_rows(conn) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sources ORDER BY ordinal"
    ).fetchall()


def latest_sources_by_role(conn) -> Dict[str, sqlite3.Row]:
    out: Dict[str, sqlite3.Row] = {}
    for row in source_rows(conn):
        old = out.get(row["role"])
        if old is None or row["version"] > old["version"]:
            out[row["role"]] = row
    return out
