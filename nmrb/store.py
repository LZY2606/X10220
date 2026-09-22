"""SQLite persistence: immutable source facts + append-only event log.

Facts (sources, peaks, edges, sites...) are written once and never
overwritten.  Revisions are new rows linked through ``parent_source_id``.
All state changes (imports, generations, decisions, merges, checkouts)
are events; the working state is reconstructed by replay (see
:mod:`nmrb.replay`).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SCHEMA_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id        TEXT PRIMARY KEY,
    role             TEXT NOT NULL,
    kind             TEXT NOT NULL,
    filename         TEXT NOT NULL,
    version          INTEGER NOT NULL,
    parent_source_id TEXT,
    content          TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    parsed_json      TEXT NOT NULL,
    import_event_id   TEXT NOT NULL,
    ordinal          INTEGER NOT NULL,
    UNIQUE(role, version)
);

CREATE TABLE IF NOT EXISTS peaks (
    peak_id        TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    local_id       TEXT NOT NULL,
    nucleus        TEXT NOT NULL,
    shift_ppm      REAL,
    shift_raw      TEXT NOT NULL,
    shift_unit     TEXT NOT NULL DEFAULT 'ppm',
    intensity      REAL,
    intensity_raw  TEXT NOT NULL,
    intensity_unit TEXT,
    integral       REAL,
    integral_raw   TEXT NOT NULL,
    integral_unit  TEXT,
    multiplicity   TEXT NOT NULL,
    tags_json      TEXT NOT NULL DEFAULT '[]',
    tags_raw       TEXT NOT NULL DEFAULT '',
    ordinal        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id         TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    local_id        TEXT NOT NULL,
    edge_kind       TEXT NOT NULL,
    from_peak_id    TEXT,
    to_peak_id      TEXT,
    from_ref        TEXT NOT NULL,
    to_ref          TEXT NOT NULL,
    from_ppm        REAL,
    to_ppm          REAL,
    strength        REAL,
    strength_raw    TEXT NOT NULL,
    strength_unit   TEXT,
    status          TEXT NOT NULL,
    ordinal         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    site_id        TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    local_id       TEXT NOT NULL,
    nucleus        TEXT NOT NULL,
    element        TEXT NOT NULL,
    predicted_ppm  REAL NOT NULL,
    predicted_raw  TEXT NOT NULL,
    shift_unit     TEXT NOT NULL DEFAULT 'ppm',
    label          TEXT NOT NULL,
    bound_carbon   TEXT,
    ordinal        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    group_id   TEXT PRIMARY KEY,
    source_id  TEXT NOT NULL,
    local_id   TEXT NOT NULL,
    ordinal    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id    TEXT NOT NULL,
    site_id     TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    PRIMARY KEY (group_id, site_id)
);

CREATE TABLE IF NOT EXISTS expected_edges (
    expected_id TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    local_id    TEXT NOT NULL,
    site_a      TEXT NOT NULL,
    site_b      TEXT NOT NULL,
    edge_kind   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    ordinal     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    generation_id   TEXT PRIMARY KEY,
    algorithm       TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    params_fp       TEXT NOT NULL,
    scope_json      TEXT NOT NULL,
    direct_json     TEXT NOT NULL,
    created_event   TEXT NOT NULL,
    superseded_gen  TEXT,
    ordinal         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    TEXT PRIMARY KEY,
    generation_id   TEXT NOT NULL,
    target_key      TEXT NOT NULL,
    target_nucleus  TEXT NOT NULL,
    target_label    TEXT NOT NULL,
    peak_id         TEXT,
    shift_ppm       REAL,
    predicted_ppm   REAL NOT NULL,
    delta_ppm       REAL,
    shift_score     REAL,
    edge_score      REAL,
    total_score     REAL NOT NULL,
    rank            INTEGER NOT NULL,
    status          TEXT NOT NULL,
    multiplicity    TEXT NOT NULL DEFAULT '',
    equivalent_n    INTEGER NOT NULL DEFAULT 1,
    overlap_group   TEXT,
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    reject_json     TEXT NOT NULL DEFAULT '[]',
    notes_json      TEXT NOT NULL DEFAULT '[]',
    ordinal         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL UNIQUE,
    branch_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    actor        TEXT NOT NULL,
    tick         INTEGER NOT NULL,
    at_iso       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    parents_json TEXT NOT NULL,
    ordinal      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    branch_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    parent_branch TEXT,
    head_seq     INTEGER NOT NULL,
    created_event TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_peaks_src ON peaks(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_sites_src ON sites(source_id);
CREATE INDEX IF NOT EXISTS idx_cand_gen ON candidates(generation_id);
CREATE INDEX IF NOT EXISTS idx_events_branch ON events(branch_id, seq);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone() is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema', ?)", (SCHEMA_VERSION,)
        )
        conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )


def next_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
    return int(row["m"]) + 1


def next_tick(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "tick", 0)) + 1
