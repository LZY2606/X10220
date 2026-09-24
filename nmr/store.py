"""SQLite 持久化层：不可变源文档、版本化派生数据、事件日志。"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT NOT NULL,
  version_id TEXT PRIMARY KEY,
  version_no INTEGER NOT NULL,
  parent_version_id TEXT,
  kind TEXT NOT NULL,
  format TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  note TEXT,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peaks(
  peak_id TEXT NOT NULL,
  doc_version_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  nucleus TEXT,
  label TEXT NOT NULL,
  line_no INTEGER,
  shift_raw TEXT,
  shift_ppm REAL,
  intensity_raw TEXT,
  intensity REAL,
  integral_raw TEXT,
  integral REAL,
  multiplicity TEXT,
  coupling_raw TEXT,
  extras TEXT,
  diagnostics TEXT,
  content_hash TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  PRIMARY KEY(peak_id, doc_version_id)
);
CREATE TABLE IF NOT EXISTS correlations(
  cor_id TEXT NOT NULL,
  doc_version_id TEXT NOT NULL,
  kind TEXT,
  a_label TEXT,
  b_label TEXT,
  extras TEXT,
  diagnostics TEXT,
  content_hash TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  PRIMARY KEY(cor_id, doc_version_id)
);
CREATE TABLE IF NOT EXISTS conditions(
  cond_id TEXT PRIMARY KEY,
  version_no INTEGER NOT NULL,
  parent_cond_id TEXT,
  fields TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS params(
  param_id TEXT PRIMARY KEY,
  version_no INTEGER NOT NULL,
  parent_param_id TEXT,
  json TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites(
  site_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  expected_lo REAL NOT NULL,
  expected_hi REAL NOT NULL,
  symmetry_group TEXT,
  x REAL NOT NULL,
  y REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bonds(
  a_site TEXT NOT NULL,
  b_site TEXT NOT NULL,
  PRIMARY KEY(a_site, b_site)
);
CREATE TABLE IF NOT EXISTS candidates(
  cand_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL,
  peak_id TEXT NOT NULL,
  peak_hash TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  score REAL NOT NULL,
  shift_score REAL NOT NULL,
  coupling_score REAL NOT NULL,
  flags TEXT NOT NULL,
  evidence TEXT NOT NULL,
  algo_version TEXT NOT NULL,
  param_fp TEXT NOT NULL,
  cond_fp TEXT NOT NULL,
  cor_hashes TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  stale_since_seq INTEGER,
  stale_reason TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  dec_id TEXT PRIMARY KEY,
  cand_id TEXT NOT NULL,
  site_id TEXT NOT NULL,
  verdict TEXT NOT NULL,
  editor TEXT NOT NULL,
  rationale TEXT,
  branch TEXT NOT NULL,
  resolves TEXT,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  revoked_seq INTEGER
);
CREATE TABLE IF NOT EXISTS conflicts(
  conflict_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL,
  dec_ids TEXT NOT NULL,
  status TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  resolved_seq INTEGER
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  at TEXT NOT NULL
);
"""

TABLES = [
    "meta", "documents", "peaks", "correlations", "conditions", "params",
    "sites", "bonds", "candidates", "decisions", "conflicts", "events",
]


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def alloc_seq(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
    seq = (int(row["value"]) if row else 0) + 1
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('seq', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(seq),),
    )
    return seq


def record_event(conn, clock, etype: str, payload: dict, seq: int | None = None):
    if seq is None:
        seq = alloc_seq(conn)
    event_id = f"ev_{seq:04d}"
    conn.execute(
        "INSERT INTO events(seq, event_id, type, payload, at) VALUES(?,?,?,?,?)",
        (seq, event_id, etype, json.dumps(payload, ensure_ascii=False), clock.now()),
    )
    return seq, event_id


def wipe(conn):
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
