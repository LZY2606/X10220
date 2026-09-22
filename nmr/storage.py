"""SQLite 持久化：仅追加事件日志 + 原始层/骨架物化表。

- ``events`` 为唯一真相来源，append-only，含 parent_id 形成事件树（分支）；
- ``sources``/``peaks``/``edges``/``atoms``/``bonds`` 是原始不可变层的物化视图，
  由事件重放重建，行内保存原始表示与来源关系；
- 计算结果（候选/未知）与决定不建可变业务表，读时由重放事件得到，
  保证“回到旧事件即恢复当时版本与结论”。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    branch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    parent_id TEXT,
    actor TEXT NOT NULL,
    clock TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    undo_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, seq);
CREATE TABLE IF NOT EXISTS sources (
    stable_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_version INTEGER NOT NULL,
    parent_source_id TEXT,
    raw_text TEXT NOT NULL,
    digest TEXT NOT NULL,
    introduced_event TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    field_name TEXT,
    file_index INTEGER,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS peaks (
    stable_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    key TEXT NOT NULL,
    nucleus TEXT NOT NULL,
    shift_raw TEXT, shift_value REAL, shift_unit TEXT, shift_basis TEXT,
    shift_convertible INTEGER NOT NULL,
    intensity_raw TEXT, intensity_value REAL, intensity_unit TEXT,
    intensity_convertible INTEGER,
    integration_raw TEXT, integration_value REAL, integration_unit TEXT,
    integration_convertible INTEGER,
    multiplicity_raw TEXT, multiplicity_value TEXT, multiplicity_convertible INTEGER,
    raw_row TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    stable_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    key TEXT NOT NULL,
    experiment TEXT NOT NULL,
    from_ref TEXT NOT NULL,
    to_ref TEXT NOT NULL,
    from_axis_value REAL, from_axis_basis TEXT,
    to_axis_value REAL, to_axis_basis TEXT,
    intensity_value REAL,
    raw_row TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atoms (
    stable_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    label TEXT NOT NULL,
    element TEXT NOT NULL,
    nucleus TEXT NOT NULL,
    symmetry_group TEXT NOT NULL,
    equivalent_to TEXT,
    attached_to TEXT,
    protons INTEGER,
    predicted_value REAL,
    predicted_raw TEXT,
    predicted_convertible INTEGER NOT NULL,
    raw_row TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bonds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    atom_a TEXT NOT NULL,
    atom_b TEXT NOT NULL,
    bond_order INTEGER NOT NULL,
    raw_row TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heads (
    project_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    head_event_id TEXT NOT NULL,
    PRIMARY KEY (project_id, branch_id)
);
"""


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _qj(text: Any) -> Optional[str]:
    if text is None:
        return None
    return _j(text)


def _qraw(text: Any) -> Optional[str]:
    if text is None:
        return None
    return str(text)


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- meta / heads -------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_head(self, project_id: str, branch_id: str, event_id: str) -> None:
        self.conn.execute(
            "INSERT INTO heads(project_id,branch_id,head_event_id) VALUES(?,?,?) "
            "ON CONFLICT(project_id,branch_id) DO UPDATE SET head_event_id=excluded.head_event_id",
            (project_id, branch_id, event_id))
        self.conn.commit()

    def get_head(self, project_id: str, branch_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT head_event_id FROM heads WHERE project_id=? AND branch_id=?",
            (project_id, branch_id)).fetchone()
        return row["head_event_id"] if row else None

    def list_heads(self, project_id: str) -> List[Dict[str, str]]:
        rows = self.conn.execute(
            "SELECT branch_id, head_event_id FROM heads WHERE project_id=?",
            (project_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- events -------------------------------------------------------
    def append_event(self, event: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO events(event_id,project_id,seq,branch_id,kind,parent_id,"
            "actor,clock,fingerprint,payload_json,undo_of) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (event["event_id"], event["project_id"], event["seq"], event["branch_id"],
             event["kind"], event.get("parent_id"), event["actor"], event["clock"],
             event["fingerprint"], _j(event["payload"]), event.get("undo_of")))
        self.conn.commit()

    def all_events(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq, event_id",
            (project_id,)).fetchall()
        return [self._event_row(r) for r in rows]

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?",
                                (event_id,)).fetchone()
        return self._event_row(row) if row else None

    @staticmethod
    def _event_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "seq": row["seq"],
            "branch_id": row["branch_id"],
            "kind": row["kind"],
            "parent_id": row["parent_id"],
            "actor": row["actor"],
            "clock": row["clock"],
            "fingerprint": row["fingerprint"],
            "payload": json.loads(row["payload_json"]),
            "undo_of": row["undo_of"],
        }

    # ---- raw materialisation ------------------------------------------
    def insert_source(self, record: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO sources(stable_id,project_id,kind,filename,file_version,"
            "parent_source_id,raw_text,digest,introduced_event) VALUES(?,?,?,?,?,?,?,?,?)",
            (record["stable_id"], record["project_id"], record["kind"],
             record["filename"], record["file_version"], record.get("parent_source_id"),
             record["raw_text"], record["digest"], record["introduced_event"]))
        for diag in record.get("diagnostics", []):
            self.conn.execute(
                "INSERT INTO source_diagnostics(source_id,severity,code,message,"
                "field_name,file_index,raw) VALUES(?,?,?,?,?,?,?)",
                (record["stable_id"], diag["severity"], diag["code"], diag["message"],
                 diag.get("field"), diag.get("file_index"), _qj(diag.get("raw"))))
        for peak in record.get("peaks", []):
            p = peak
            self.conn.execute(
                "INSERT INTO peaks(stable_id,project_id,source_id,file_index,key,nucleus,"
                "shift_raw,shift_value,shift_unit,shift_basis,shift_convertible,"
                "intensity_raw,intensity_value,intensity_unit,intensity_convertible,"
                "integration_raw,integration_value,integration_unit,integration_convertible,"
                "multiplicity_raw,multiplicity_value,multiplicity_convertible,raw_row) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["stable_id"], record["project_id"], record["stable_id"],
                 p["file_index"], p["key"], p["nucleus"],
                 _qraw(p["shift"].get("raw")), p["shift"].get("value"),
                 p["shift"].get("unit"), p["shift"].get("basis"),
                 int(p["shift"]["convertible"]),
                 _qraw((p.get("intensity") or {}).get("raw")),
                 (p.get("intensity") or {}).get("value"),
                 (p.get("intensity") or {}).get("unit"),
                 None if p.get("intensity") is None else int(p["intensity"]["convertible"]),
                 _qraw((p.get("integration") or {}).get("raw")),
                 (p.get("integration") or {}).get("value"),
                 (p.get("integration") or {}).get("unit"),
                 None if p.get("integration") is None else int(p["integration"]["convertible"]),
                 _qraw((p.get("multiplicity") or {}).get("raw")),
                 (p.get("multiplicity") or {}).get("value"),
                 None if p.get("multiplicity") is None else int(p["multiplicity"]["convertible"]),
                 _j(p.get("raw_row", {}))))
        for edge in record.get("edges", []):
            self.conn.execute(
                "INSERT INTO edges(stable_id,project_id,source_id,file_index,key,experiment,"
                "from_ref,to_ref,from_axis_value,from_axis_basis,to_axis_value,to_axis_basis,"
                "intensity_value,raw_row) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (edge["stable_id"], record["project_id"], record["stable_id"],
                 edge["file_index"], edge["key"], edge["experiment"], edge["from_ref"],
                 edge["to_ref"],
                 (edge.get("from_axis") or {}).get("value"),
                 (edge.get("from_axis") or {}).get("basis"),
                 (edge.get("to_axis") or {}).get("value"),
                 (edge.get("to_axis") or {}).get("basis"),
                 (edge.get("intensity") or {}).get("value"), _j(edge.get("raw_row", {}))))
        for atom in record.get("atoms", []):
            self.conn.execute(
                "INSERT INTO atoms(stable_id,project_id,source_id,file_index,label,element,"
                "nucleus,symmetry_group,equivalent_to,attached_to,protons,predicted_value,"
                "predicted_raw,predicted_convertible,raw_row) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (atom["stable_id"], record["project_id"], record["stable_id"],
                 atom["file_index"], atom["label"], atom["element"], atom["nucleus"],
                 atom["symmetry_group"], atom.get("equivalent_to"), atom.get("attached_to"),
                 atom.get("protons"), atom["predicted_shift"].get("value"),
                 _qraw(atom["predicted_shift"].get("raw")),
                 int(atom["predicted_shift"]["convertible"]), _j(atom.get("raw_row", {}))))
        for bond in record.get("bonds", []):
            self.conn.execute(
                "INSERT INTO bonds(project_id,source_id,file_index,atom_a,atom_b,"
                "bond_order,raw_row) VALUES(?,?,?,?,?,?,?)",
                (record["project_id"], record["stable_id"], bond["file_index"],
                 bond["from"], bond["to"], bond["order"], _j(bond.get("raw_row", {}))))
        self.conn.commit()

    # ---- read raw layer -----------------------------------------------
    def list_sources(self, project_id: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM sources WHERE project_id=? ORDER BY file_version, kind, filename",
            (project_id,)).fetchall())

    def get_source(self, stable_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sources WHERE stable_id=?",
                                 (stable_id,)).fetchone()

    def list_diagnostics(self, source_id: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM source_diagnostics WHERE source_id=? ORDER BY id",
            (source_id,)).fetchall())

    def list_peaks(self, project_id: str, source_id: str = None) -> List[sqlite3.Row]:
        if source_id:
            return list(self.conn.execute(
                "SELECT * FROM peaks WHERE project_id=? AND source_id=? "
                "ORDER BY file_index, stable_id", (project_id, source_id)).fetchall())
        return list(self.conn.execute(
            "SELECT * FROM peaks WHERE project_id=? ORDER BY file_index, stable_id",
            (project_id,)).fetchall())

    def list_edges(self, project_id: str, source_id: str = None) -> List[sqlite3.Row]:
        if source_id:
            return list(self.conn.execute(
                "SELECT * FROM edges WHERE project_id=? AND source_id=? "
                "ORDER BY file_index, stable_id", (project_id, source_id)).fetchall())
        return list(self.conn.execute(
            "SELECT * FROM edges WHERE project_id=? ORDER BY file_index, stable_id",
            (project_id,)).fetchall())

    def list_atoms(self, project_id: str, source_id: str = None) -> List[sqlite3.Row]:
        if source_id:
            return list(self.conn.execute(
                "SELECT * FROM atoms WHERE project_id=? AND source_id=? "
                "ORDER BY file_index, stable_id", (project_id, source_id)).fetchall())
        return list(self.conn.execute(
            "SELECT * FROM atoms WHERE project_id=? ORDER BY file_index, stable_id",
            (project_id,)).fetchall())

    def list_bonds(self, project_id: str, source_id: str = None) -> List[sqlite3.Row]:
        if source_id:
            return list(self.conn.execute(
                "SELECT * FROM bonds WHERE project_id=? AND source_id=? ORDER BY file_index, id",
                (project_id, source_id)).fetchall())
        return list(self.conn.execute(
            "SELECT * FROM bonds WHERE project_id=? ORDER BY file_index, id",
            (project_id,)).fetchall())

    # ---- project lifecycle --------------------------------------------
    def count_events(self, project_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE project_id=?", (project_id,)).fetchone()
        return int(row["n"])

    def list_projects(self) -> List[str]:
        rows = self.conn.execute("SELECT DISTINCT project_id FROM events").fetchall()
        return [r["project_id"] for r in rows]

    def delete_project(self, project_id: str) -> None:
        for table in ("heads", "bonds", "atoms", "edges", "peaks",
                      "source_diagnostics", "sources", "events"):
            self.conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        self.conn.commit()

    def list_event_ids(self, project_id: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT event_id FROM events WHERE project_id=? ORDER BY seq",
            (project_id,)).fetchall()
        return [r["event_id"] for r in rows]
