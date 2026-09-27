from __future__ import annotations

import json

from .clock import now_iso
from .db import next_id, next_seq, row


def emit(conn, project_id, branch_id, event_type, payload) -> dict:
    """Append a machine-readable event to the project log."""
    seq = next_seq(conn)
    event_id = next_id(conn, "ev")
    parent = row(
        conn,
        "SELECT id FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
        (project_id,),
    )
    conn.execute(
        "INSERT INTO events(id,project_id,seq,ts,branch_id,type,payload,parent_event_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            event_id,
            project_id,
            seq,
            now_iso(),
            branch_id,
            event_type,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            parent["id"] if parent else None,
        ),
    )
    return {"id": event_id, "seq": seq}
