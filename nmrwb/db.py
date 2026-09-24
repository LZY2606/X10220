"""SQLite schema and low-level helpers.

Design notes:
- Raw inputs are immutable: they are inserted once and never updated.
  Revisions are new rows with a parent pointer and a bumped version.
- Every state transition is an event row with a per-project monotonic
  sequence number, so any historical view can be reconstructed.
- Ids are deterministic per database (kind-NNNNNN) which keeps exports
  stable and diff-friendly.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters(
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  parent_branch_id TEXT,
  fork_event_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inputs(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  version INTEGER NOT NULL,
  parent_id TEXT,
  format TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  commit_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peaks(
  id TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  ord INTEGER NOT NULL,
  label TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  shift_raw TEXT,
  shift_ppm REAL,
  intensity_raw TEXT,
  intensity REAL,
  integration_raw TEXT,
  integration REAL,
  multiplicity TEXT,
  unit TEXT,
  basis TEXT,
  diagnostic TEXT
);
CREATE TABLE IF NOT EXISTS edges(
  id TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  ord INTEGER NOT NULL,
  kind TEXT NOT NULL,
  a_label TEXT NOT NULL,
  b_label TEXT NOT NULL,
  weight REAL,
  raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atoms(
  id TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  ord INTEGER NOT NULL,
  atom_key TEXT NOT NULL,
  element TEXT NOT NULL,
  shift_center REAL,
  shift_width REAL,
  symmetry_group TEXT,
  bound_carbon TEXT,
  x REAL,
  y REAL,
  unit TEXT,
  basis TEXT
);
CREATE TABLE IF NOT EXISTS bonds(
  id TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  ord INTEGER NOT NULL,
  a_key TEXT NOT NULL,
  b_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conditions(
  id TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  solvent TEXT,
  frequency_mhz REAL,
  temperature_c REAL,
  reference TEXT,
  shift_bias_ppm REAL NOT NULL DEFAULT 0.0,
  raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  branch_id TEXT,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  actor TEXT,
  at TEXT NOT NULL,
  parent_event_id TEXT
);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  peak_label TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  atom_key TEXT NOT NULL,
  score REAL,
  status TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  algo_version TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  conditions_id TEXT,
  created_seq INTEGER NOT NULL,
  stale_reason TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  peak_label TEXT NOT NULL,
  editor TEXT NOT NULL,
  decision TEXT NOT NULL,
  event_id TEXT NOT NULL,
  at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_inputs_project ON inputs(project_id, kind, version);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id, peak_label);
"""


def connect(path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def _bump(con, name):
    con.execute(
        "INSERT INTO counters(name, value) VALUES(?, 0) ON CONFLICT(name) DO NOTHING",
        (name,),
    )
    con.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    return con.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[
        "value"
    ]


def new_id(con, kind):
    return "%s-%06d" % (kind, _bump(con, "id"))


def next_seq(con, project_id):
    return _bump(con, "seq:%s" % project_id)


def peek_seq(con, project_id):
    row = con.execute(
        "SELECT value FROM counters WHERE name = ?", ("seq:%s" % project_id,)
    ).fetchone()
    return row["value"] if row else 0
