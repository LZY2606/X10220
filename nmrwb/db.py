import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  seq_counter INTEGER NOT NULL DEFAULT 0,
  created_seq INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  version INTEGER NOT NULL,
  parent_id TEXT,
  format_version TEXT,
  raw_text TEXT NOT NULL,
  normalized TEXT,
  actor TEXT,
  content_hash TEXT,
  created_seq INTEGER NOT NULL,
  created_at REAL NOT NULL,
  superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS diagnostics(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  field TEXT,
  raw_value TEXT,
  message TEXT,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  algo_version TEXT NOT NULL,
  params_json TEXT NOT NULL,
  params_fingerprint TEXT NOT NULL,
  source_ids TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expired_seq INTEGER,
  superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  nucleus_id TEXT NOT NULL,
  peak_id TEXT,
  score REAL,
  status TEXT NOT NULL,
  reasons TEXT NOT NULL,
  evidence TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  expired_seq INTEGER
);
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  parent_id TEXT,
  origin_seq INTEGER,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  nucleus_id TEXT NOT NULL,
  candidate_id TEXT,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  base_seq INTEGER NOT NULL,
  note TEXT,
  created_seq INTEGER NOT NULL,
  created_at REAL NOT NULL,
  superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  branch_id TEXT,
  type TEXT NOT NULL,
  actor TEXT,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
