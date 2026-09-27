from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    seq INTEGER NOT NULL,
    head_branch_id TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    seq INTEGER NOT NULL,
    fork_event_seq INTEGER,
    label TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    branch_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    parent_event_id TEXT
);
CREATE TABLE IF NOT EXISTS params_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    seq INTEGER NOT NULL,
    params TEXT NOT NULL,
    event_id TEXT,
    superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS source_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    seq INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    event_id TEXT,
    superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS peaks (
    id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    label TEXT NOT NULL,
    nucleus_raw TEXT NOT NULL,
    shift_raw TEXT NOT NULL,
    shift_ppm REAL,
    intensity_raw TEXT NOT NULL,
    intensity REAL,
    integration_raw TEXT NOT NULL,
    integration REAL,
    multiplicity TEXT NOT NULL,
    diag TEXT
);
CREATE TABLE IF NOT EXISTS correlations (
    id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    a_label TEXT NOT NULL,
    b_label TEXT NOT NULL,
    a_peak_id TEXT,
    b_peak_id TEXT,
    diag TEXT
);
CREATE TABLE IF NOT EXISTS conditions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    seq INTEGER NOT NULL,
    solvent TEXT NOT NULL,
    temperature_raw TEXT NOT NULL,
    temperature_k REAL,
    reference TEXT NOT NULL,
    freq_mhz_raw TEXT NOT NULL,
    freq_mhz REAL,
    diag TEXT,
    event_id TEXT,
    superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS atoms (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    label TEXT NOT NULL,
    element TEXT NOT NULL,
    expected_ppm REAL NOT NULL,
    window_ppm REAL NOT NULL,
    sym_group TEXT,
    x REAL NOT NULL,
    y REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    a_atom_id TEXT NOT NULL,
    b_atom_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    branch_id TEXT,
    seq INTEGER NOT NULL,
    algo_version TEXT NOT NULL,
    param_fp TEXT NOT NULL,
    deps TEXT NOT NULL,
    report TEXT NOT NULL,
    event_id TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    peak_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    score REAL NOT NULL,
    reasons TEXT NOT NULL,
    deps TEXT NOT NULL,
    stale_seq INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    candidate_id TEXT,
    action TEXT NOT NULL,
    editor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    undone_seq INTEGER,
    event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id);
CREATE INDEX IF NOT EXISTS idx_sources_project ON source_versions(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    conn.execute(
        "INSERT INTO counters(name,value) VALUES(?,1) "
        "ON CONFLICT(name) DO UPDATE SET value=value+1",
        (prefix,),
    )
    value = conn.execute(
        "SELECT value FROM counters WHERE name=?", (prefix,)
    ).fetchone()[0]
    return f"{prefix}-{value:06d}"


def next_seq(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO counters(name,value) VALUES('seq',1) "
        "ON CONFLICT(name) DO UPDATE SET value=value+1"
    )
    return conn.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]


def rows(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(conn: sqlite3.Connection, sql: str, args=()) -> dict | None:
    found = conn.execute(sql, args).fetchone()
    return dict(found) if found else None
