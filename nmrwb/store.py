"""SQLite 持久层：不可变输入版本、派生候选、状态历史、分支、事件日志。

所有对象都有稳定身份（*_id / *_key），导出再导入后保持不变。
seq 是全局单调序号，用于重放（replay）到任意历史事件时刻。
"""
import json
import sqlite3
import threading

from .clock import now_iso

MAIN_BRANCH = "main"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS inputs (
  input_id TEXT PRIMARY KEY,
  stream_id TEXT NOT NULL,
  parent_id TEXT,
  version_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  label TEXT,
  raw_text TEXT NOT NULL,
  actor TEXT,
  created_at TEXT,
  seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS peaks (
  peak_key TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  local_id TEXT NOT NULL,
  nucleus TEXT,
  shift_raw TEXT, shift_ppm REAL,
  intensity_raw TEXT, intensity REAL,
  integral_raw TEXT, integral REAL,
  multiplicity TEXT,
  diag TEXT
);
CREATE TABLE IF NOT EXISTS edges (
  edge_key TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  local_id TEXT NOT NULL,
  kind TEXT, a TEXT, b TEXT,
  strength_raw TEXT, strength REAL, diag TEXT
);
CREATE TABLE IF NOT EXISTS params (
  input_id TEXT PRIMARY KEY,
  solvent TEXT,
  temperature_raw TEXT, temperature_K REAL,
  reference TEXT,
  freq_raw TEXT, freq_mhz REAL,
  diag TEXT
);
CREATE TABLE IF NOT EXISTS molecules (input_id TEXT PRIMARY KEY, name TEXT, diag TEXT);
CREATE TABLE IF NOT EXISTS atoms (
  atom_key TEXT PRIMARY KEY,
  input_id TEXT NOT NULL,
  atom_id TEXT NOT NULL,
  element TEXT, x REAL, y REAL,
  h_shift REAL, c_shift REAL, nh REAL, mult TEXT,
  sym_group TEXT, attached TEXT
);
CREATE TABLE IF NOT EXISTS bonds (input_id TEXT NOT NULL, a TEXT, b TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  cand_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  peak_key TEXT,
  score REAL,
  base_status TEXT NOT NULL,
  evidence TEXT NOT NULL,
  algo_version TEXT NOT NULL,
  params_fp TEXT NOT NULL,
  param_fields TEXT NOT NULL,
  sources TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS cand_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch_id TEXT NOT NULL,
  cand_id TEXT NOT NULL,
  status TEXT NOT NULL,
  seq INTEGER NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY,
  branch_id TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  cand_id TEXT,
  action TEXT NOT NULL,
  actor TEXT,
  note TEXT,
  seq INTEGER NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS branches (
  branch_id TEXT PRIMARY KEY,
  name TEXT,
  parent_branch TEXT,
  fork_event TEXT,
  created_seq INTEGER,
  created_at TEXT,
  head_event TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  branch_id TEXT,
  parent_event TEXT,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  actor TEXT,
  created_at TEXT
);
"""

TABLES = ["meta", "inputs", "peaks", "edges", "params", "molecules", "atoms",
          "bonds", "candidates", "cand_status", "decisions", "branches", "events"]


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            if not self.conn.execute(
                    "SELECT 1 FROM branches WHERE branch_id=?", (MAIN_BRANCH,)).fetchone():
                seq = self._next_seq()
                self.conn.execute(
                    "INSERT INTO branches VALUES(?,?,?,?,?,?,?)",
                    (MAIN_BRANCH, "main", None, None, seq, now_iso(), None))
                self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---- 基础 ----
    def _next_seq(self):
        row = self.conn.execute("SELECT v FROM meta WHERE k='seq'").fetchone()
        seq = (int(row["v"]) if row else 0) + 1
        self.conn.execute(
            "INSERT INTO meta(k,v) VALUES('seq',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(seq),))
        return seq

    def new_id(self, prefix):
        return "%s-%06d" % (prefix, self._next_seq())

    def emit(self, type_, payload, branch_id=None, actor=None):
        with self.lock:
            seq = self._next_seq()
            eid = "ev-%06d" % seq
            parent = None
            if branch_id:
                br = self.get_branch(branch_id)
                parent = br["head_event"] if br else None
            self.conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (eid, seq, branch_id, parent, type_,
                 json.dumps(payload, ensure_ascii=False), actor, now_iso()))
            if branch_id:
                self.conn.execute("UPDATE branches SET head_event=? WHERE branch_id=?",
                                  (eid, branch_id))
            self.conn.commit()
            return self.get_event(eid)

    # ---- 输入版本 ----
    def add_input(self, kind, label, raw_text, actor, parent_id=None):
        with self.lock:
            stream_id, version_no = None, 1
            if parent_id:
                parent = self.get_input(parent_id)
                if not parent:
                    raise ValueError("parent input not found: %s" % parent_id)
                if parent["kind"] != kind:
                    raise ValueError("parent kind mismatch: %s != %s"
                                     % (parent["kind"], kind))
                stream_id, version_no = parent["stream_id"], parent["version_no"] + 1
            seq = self._next_seq()
            input_id = "in-%06d" % seq
            if stream_id is None:
                stream_id = input_id
            self.conn.execute(
                "INSERT INTO inputs VALUES(?,?,?,?,?,?,?,?,?)",
                (input_id, stream_id, parent_id, version_no, kind, label,
                 raw_text, actor, now_iso(), seq))
            self.conn.commit()
            return self.get_input(input_id)

    def get_input(self, input_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM inputs WHERE input_id=?",
                                  (input_id,)).fetchone()
            return dict(r) if r else None

    def all_inputs(self):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM inputs ORDER BY seq"))

    def head_inputs(self, max_seq=None):
        """每个 (kind, stream) 的最新版本；max_seq 用于重放历史时刻。"""
        heads = {}
        for row in self.all_inputs():
            if max_seq is not None and row["seq"] > max_seq:
                continue
            key = (row["kind"], row["stream_id"])
            if key not in heads or row["version_no"] > heads[key]["version_no"]:
                heads[key] = row
        return sorted(heads.values(), key=lambda r: r["seq"])

    # ---- 解析产物 ----
    def add_peaks(self, input_id, peaks):
        with self.lock:
            for p in peaks:
                self.conn.execute(
                    "INSERT INTO peaks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (p["peak_key"], input_id, p["local_id"], p.get("nucleus"),
                     p.get("shift_raw"), p.get("shift_ppm"),
                     p.get("intensity_raw"), p.get("intensity"),
                     p.get("integral_raw"), p.get("integral"),
                     p.get("multiplicity"), json.dumps(p.get("diag") or {},
                                                     ensure_ascii=False)))
            self.conn.commit()

    def add_edges(self, input_id, edges):
        with self.lock:
            for e in edges:
                self.conn.execute(
                    "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?)",
                    (e["edge_key"], input_id, e["local_id"], e.get("kind"),
                     e.get("a"), e.get("b"), e.get("strength_raw"),
                     e.get("strength"),
                     json.dumps(e.get("diag") or {}, ensure_ascii=False)))
            self.conn.commit()

    def add_params(self, input_id, params):
        with self.lock:
            self.conn.execute(
                "INSERT INTO params VALUES(?,?,?,?,?,?,?,?)",
                (input_id, params.get("solvent"), params.get("temperature_raw"),
                 params.get("temperature_K"), params.get("reference"),
                 params.get("freq_raw"), params.get("freq_mhz"),
                 json.dumps(params.get("diag") or {}, ensure_ascii=False)))
            self.conn.commit()

    def add_molecule(self, input_id, name, atoms, bonds, diag=None):
        with self.lock:
            self.conn.execute("INSERT INTO molecules VALUES(?,?,?)",
                              (input_id, name, json.dumps(diag or {},
                                                          ensure_ascii=False)))
            for a in atoms:
                self.conn.execute(
                    "INSERT INTO atoms VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (a["atom_key"], input_id, a["atom_id"], a.get("element"),
                     a.get("x"), a.get("y"), a.get("h_shift"), a.get("c_shift"),
                     a.get("nh"), a.get("mult"), a.get("sym_group"),
                     a.get("attached")))
            for b in bonds:
                self.conn.execute("INSERT INTO bonds VALUES(?,?,?)",
                                  (input_id, b[0], b[1]))
            self.conn.commit()

    def peaks_of(self, input_id):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM peaks WHERE input_id=? ORDER BY local_id",
                (input_id,)))

    def edges_of(self, input_id):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM edges WHERE input_id=? ORDER BY local_id",
                (input_id,)))

    def params_of(self, input_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM params WHERE input_id=?",
                                  (input_id,)).fetchone()
            return dict(r) if r else None

    def atoms_of(self, input_id):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM atoms WHERE input_id=? ORDER BY atom_id",
                (input_id,)))

    def bonds_of(self, input_id):
        with self.lock:
            return [tuple(r) for r in self.conn.execute(
                "SELECT a,b FROM bonds WHERE input_id=?", (input_id,)).fetchall()]

    # ---- 候选 ----
    def add_candidate(self, cand):
        with self.lock:
            seq = self._next_seq()
            cid = "ca-%06d" % seq
            self.conn.execute(
                "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, cand["run_id"], cand["nucleus"], cand.get("peak_key"),
                 cand.get("score"), cand["base_status"], cand["evidence"],
                 cand["algo_version"], cand["params_fp"], cand["param_fields"],
                 cand["sources"], seq, now_iso()))
            self.conn.commit()
            return self.get_candidate(cid)

    def get_candidate(self, cand_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM candidates WHERE cand_id=?",
                                  (cand_id,)).fetchone()
            return dict(r) if r else None

    def candidates_all(self):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM candidates ORDER BY created_seq"))

    def candidates_for_nucleus(self, nucleus):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM candidates WHERE nucleus=? ORDER BY score DESC",
                (nucleus,)))

    def set_status(self, branch_id, cand_id, status, reason=None):
        with self.lock:
            seq = self._next_seq()
            self.conn.execute(
                "INSERT INTO cand_status(branch_id,cand_id,status,seq,reason)"
                " VALUES(?,?,?,?,?)", (branch_id, cand_id, status, seq, reason))
            self.conn.commit()
            return seq

    # ---- 分支 ----
    def get_branch(self, branch_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM branches WHERE branch_id=?",
                                  (branch_id,)).fetchone()
            return dict(r) if r else None

    def branches_all(self):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM branches ORDER BY created_seq"))

    def fork_branch(self, parent_branch, name=None):
        with self.lock:
            pb = self.get_branch(parent_branch)
            if not pb:
                raise ValueError("branch not found: %s" % parent_branch)
            seq = self._next_seq()
            bid = "br-%06d" % seq
            self.conn.execute(
                "INSERT INTO branches VALUES(?,?,?,?,?,?,?)",
                (bid, name or bid, parent_branch, pb["head_event"], seq,
                 now_iso(), pb["head_event"]))
            self.conn.commit()
            return self.get_branch(bid)

    def branch_chain(self, branch_id):
        chain, cur = [], branch_id
        while cur:
            chain.append(cur)
            br = self.get_branch(cur)
            cur = br["parent_branch"] if br else None
        return chain

    def resolve_status(self, cand, branch_id, max_seq=None):
        chain = self.branch_chain(branch_id)
        marks = ",".join("?" * len(chain))
        sql = ("SELECT status FROM cand_status WHERE cand_id=? "
               "AND branch_id IN (%s)" % marks)
        args = [cand["cand_id"]] + chain
        if max_seq is not None:
            sql += " AND seq<=?"
            args.append(max_seq)
        sql += " ORDER BY seq DESC LIMIT 1"
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return r["status"] if r else cand["base_status"]

    # ---- 决定 ----
    def add_decision(self, branch_id, nucleus, cand_id, action, actor, note=None):
        with self.lock:
            seq = self._next_seq()
            did = "de-%06d" % seq
            self.conn.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)",
                (did, branch_id, nucleus, cand_id, action, actor, note,
                 seq, now_iso()))
            self.conn.commit()
            return self.get_decision(did)

    def get_decision(self, decision_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM decisions WHERE decision_id=?",
                                  (decision_id,)).fetchone()
            return dict(r) if r else None

    def decisions_all(self):
        with self.lock:
            return _rows(self.conn.execute("SELECT * FROM decisions ORDER BY seq"))

    def decisions_on_branch(self, branch_id):
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM decisions WHERE branch_id=? ORDER BY seq",
                (branch_id,)))

    # ---- 事件 ----
    def get_event(self, event_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM events WHERE event_id=?",
                                  (event_id,)).fetchone()
            return dict(r) if r else None

    def events_all(self):
        with self.lock:
            return _rows(self.conn.execute("SELECT * FROM events ORDER BY seq"))

    # ---- 导出 / 导入 ----
    def export_data(self):
        with self.lock:
            tables = {}
            for t in TABLES:
                order = "rowid"
                tables[t] = _rows(self.conn.execute(
                    "SELECT * FROM %s ORDER BY %s" % (t, order)))
            return {"format": "nmrwb-export", "version": 1, "tables": tables}

    def is_empty(self):
        with self.lock:
            return not self.conn.execute("SELECT 1 FROM inputs LIMIT 1").fetchone()

    def import_data(self, data):
        if data.get("format") != "nmrwb-export":
            raise ValueError("not an nmrwb export")
        with self.lock:
            if not self.is_empty():
                raise ValueError("target database is not empty")
            for t in TABLES:
                rows = data["tables"].get(t, [])
                if not rows:
                    continue
                cols = list(rows[0].keys())
                sql = "INSERT INTO %s(%s) VALUES(%s)" % (
                    t, ",".join(cols), ",".join("?" * len(cols)))
                self.conn.executemany(sql, [[r.get(c) for c in cols]
                                            for r in rows])
            self.conn.commit()
