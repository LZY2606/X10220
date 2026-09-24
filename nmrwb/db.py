"""SQLite 结构。所有行只追加或就地存储实体，当前状态由事件重放得到。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    parent_id TEXT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, seq);

CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    raw_text TEXT NOT NULL,
    fmt TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peaks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    spectrum TEXT NOT NULL,
    label TEXT NOT NULL,
    raw TEXT NOT NULL,
    norm TEXT,
    diagnostic TEXT
);

CREATE TABLE IF NOT EXISTS correlations (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    spectrum TEXT,
    kind TEXT NOT NULL,
    a_label TEXT NOT NULL,
    b_label TEXT NOT NULL,
    raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS molecules (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    data TEXT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS params (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    values_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    peak_id TEXT NOT NULL,
    score REAL NOT NULL,
    flags TEXT NOT NULL,
    evidence TEXT NOT NULL,
    reasons TEXT NOT NULL,
    algo_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (project_id, kind)
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
