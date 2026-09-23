"""SQLite persistence layer. One file per workbench instance; no external services."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS atoms (
    project_id TEXT NOT NULL,
    id TEXT NOT NULL,
    label TEXT NOT NULL,
    element TEXT NOT NULL,
    expected_shift REAL,
    shift_tol REAL,
    expected_mult TEXT,
    symmetry_group TEXT,
    x REAL NOT NULL DEFAULT 0,
    y REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, id)
);
CREATE TABLE IF NOT EXISTS bonds (
    project_id TEXT NOT NULL,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    PRIMARY KEY (project_id, a, b)
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    format_version TEXT NOT NULL,
    headers TEXT NOT NULL DEFAULT '{}',
    parent_id TEXT,
    seq INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system'
);
CREATE TABLE IF NOT EXISTS peaks (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    label TEXT NOT NULL,
    shift_raw TEXT,
    shift_ppm REAL,
    intensity_raw TEXT,
    integral_raw TEXT,
    multiplicity TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    diagnostic TEXT,
    line_no INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    weight REAL,
    line_no INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS params (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    payload TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    seq INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system'
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    peak_id TEXT,
    score REAL NOT NULL,
    state TEXT NOT NULL,
    flags TEXT NOT NULL DEFAULT '[]',
    reasons TEXT NOT NULL DEFAULT '[]',
    components TEXT NOT NULL DEFAULT '{}',
    algo_version TEXT NOT NULL,
    param_fingerprint TEXT NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '[]',
    gen_seq INTEGER NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    branch_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    logical_time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    base_seq INTEGER NOT NULL,
    created_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
