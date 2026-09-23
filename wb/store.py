"""SQLite-backed event store.

Everything is append-only and carries a global monotonically increasing `seq`.
Stable identities (doc_id@version, der-NNNNNN, evt-NNNNNN, b-NNNNNN) survive
export -> delete -> import round-trips unchanged, together with order and
provenance.
"""

import hashlib
import json
import sqlite3

from .clock import SystemClock

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT NOT NULL, version INTEGER NOT NULL, parent_version INTEGER,
  kind TEXT NOT NULL, name TEXT NOT NULL, format TEXT NOT NULL,
  content TEXT NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL,
  created_at TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(doc_id, version));
CREATE TABLE IF NOT EXISTS derivations(
  der_id TEXT PRIMARY KEY, kind TEXT NOT NULL, algo_version TEXT NOT NULL,
  params_fp TEXT NOT NULL, input_refs TEXT NOT NULL, seq INTEGER NOT NULL,
  created_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  kind TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '', at TEXT NOT NULL,
  payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS branches(
  branch_id TEXT PRIMARY KEY, parent TEXT, created_seq INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '');
"""

EXPORT_FORMAT = "wb-export/1"


def _row(r):
    return {k: r[k] for k in r.keys()}


class Store:
    def __init__(self, path, clock=None):
        self.clock = clock or SystemClock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if self.conn.execute("SELECT COUNT(*) c FROM counters").fetchone()["c"] == 0:
            self.conn.execute("INSERT INTO counters VALUES('seq', 0)")
            self.conn.execute("INSERT INTO branches VALUES('b-main', NULL, 0, 'main')")
            self.conn.commit()

    # -- sequencing -----------------------------------------------------
    def current_seq(self) -> int:
        return self.conn.execute(
            "SELECT value FROM counters WHERE name='seq'").fetchone()["value"]

    def next_seq(self) -> int:
        self.conn.execute("UPDATE counters SET value=value+1 WHERE name='seq'")
        self.conn.commit()
        return self.current_seq()

    # -- events ---------------------------------------------------------
    def add_event(self, kind, payload, actor="", branch="b-main", seq=None):
        seq = seq if seq is not None else self.next_seq()
        event_id = f"evt-{seq:06d}"
        at = self.clock.now()
        self.conn.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (seq, event_id, branch, kind, actor, at,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()
        return {"seq": seq, "event_id": event_id, "branch_id": branch,
                "kind": kind, "actor": actor, "at": at, "payload": payload}

    def events_upto(self, seq=None):
        q = "SELECT * FROM events"
        args = ()
        if seq is not None:
            q += " WHERE seq<=?"
            args = (seq,)
        q += " ORDER BY seq"
        out = []
        for r in self.conn.execute(q, args):
            d = _row(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # -- branches ---------------------------------------------------------
    def insert_branch(self, branch_id, parent, created_seq, label=""):
        self.conn.execute("INSERT INTO branches VALUES(?,?,?,?)",
                          (branch_id, parent, created_seq, label))
        self.conn.commit()

    def branches(self):
        return [_row(r) for r in self.conn.execute(
            "SELECT * FROM branches ORDER BY created_seq, branch_id")]

    def branch_parent(self, branch_id):
        r = self.conn.execute("SELECT parent FROM branches WHERE branch_id=?",
                              (branch_id,)).fetchone()
        return r["parent"] if r else None

    def branch_lineage(self, branch_id):
        chain, b = [], branch_id
        while b:
            chain.append(b)
            b = self.branch_parent(b)
        return list(reversed(chain))

    # -- documents (immutable raw inputs, versioned) ----------------------
    def latest_document(self, doc_id, seq=None):
        q = "SELECT * FROM documents WHERE doc_id=?"
        args = [doc_id]
        if seq is not None:
            q += " AND seq<=?"
            args.append(seq)
        q += " ORDER BY version DESC LIMIT 1"
        r = self.conn.execute(q, args).fetchone()
        return _row(r) if r else None

    def latest_by_kind(self, kind, seq=None):
        q = ("SELECT * FROM documents d WHERE kind=? "
             "AND version=(SELECT MAX(version) FROM documents "
             "             WHERE doc_id=d.doc_id")
        args = [kind]
        if seq is not None:
            q += " AND seq<=?"
            args.append(seq)
        q += ")"
        if seq is not None:
            q += " AND seq<=?"
            args.append(seq)
        r = self.conn.execute(q, args).fetchone()
        return _row(r) if r else None

    def documents(self, seq=None):
        q = "SELECT * FROM documents"
        args = ()
        if seq is not None:
            q += " WHERE seq<=?"
            args = (seq,)
        q += " ORDER BY seq"
        return [_row(r) for r in self.conn.execute(q, args)]

    def add_document(self, doc_id, kind, name, content, fmt, actor=""):
        """Raw inputs are immutable: identical content is a no-op, different
        content becomes a new version linked to its parent."""
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        prev = self.latest_document(doc_id)
        if prev and prev["sha"] == sha:
            return prev, False
        version = prev["version"] + 1 if prev else 1
        parent = prev["version"] if prev else None
        seq = self.next_seq()
        self.conn.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, version, parent, kind, name, fmt, content, sha, seq,
             self.clock.now(), actor))
        self.conn.commit()
        kind_of_event = "import_document" if version == 1 else "revise_document"
        self.add_event(kind_of_event, {
            "doc_id": doc_id, "version": version, "parent_version": parent,
            "kind": kind, "name": name, "sha": sha}, actor=actor)
        return self.latest_document(doc_id), True

    # -- derivations -------------------------------------------------------
    def add_derivation(self, kind, algo_version, params_fp, input_refs, payload,
                       actor=""):
        seq = self.next_seq()
        der_id = f"der-{seq:06d}"
        self.conn.execute(
            "INSERT INTO derivations VALUES(?,?,?,?,?,?,?,?,?)",
            (der_id, kind, algo_version, params_fp, json.dumps(input_refs),
             seq, self.clock.now(), "current",
             json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()
        self.add_event("compute", {
            "der_id": der_id, "kind": kind, "algo_version": algo_version,
            "params_fp": params_fp, "input_refs": input_refs,
            "candidates": len(payload.get("candidates", []))}, actor=actor)
        return der_id

    def derivations(self, seq=None):
        q = "SELECT * FROM derivations"
        args = ()
        if seq is not None:
            q += " WHERE seq<=?"
            args = (seq,)
        q += " ORDER BY seq"
        return [_row(r) for r in self.conn.execute(q, args)]

    def derivation(self, der_id):
        r = self.conn.execute("SELECT * FROM derivations WHERE der_id=?",
                              (der_id,)).fetchone()
        return _row(r) if r else None

    # -- export / import ----------------------------------------------------
    def export(self):
        return {
            "format": EXPORT_FORMAT,
            "counters": {"seq": self.current_seq()},
            "documents": self.documents(),
            "derivations": [dict(d) for d in self.derivations()],
            "events": [{k: (json.dumps(v, ensure_ascii=False, sort_keys=True)
                            if k == "payload" else v)
                        for k, v in e.items()} for e in self.events_upto()],
            "branches": self.branches(),
        }

    def load_export(self, data, actor=""):
        if data.get("format") != EXPORT_FORMAT:
            raise ValueError(f"unsupported export format: {data.get('format')}")
        was_empty = self.current_seq() == 0
        inserted = 0
        for d in data["documents"]:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (d["doc_id"], d["version"], d["parent_version"], d["kind"],
                 d["name"], d["format"], d["content"], d["sha"], d["seq"],
                 d["created_at"], d["actor"]))
            inserted += cur.rowcount
        for d in data["derivations"]:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO derivations VALUES(?,?,?,?,?,?,?,?,?)",
                (d["der_id"], d["kind"], d["algo_version"], d["params_fp"],
                 d["input_refs"], d["seq"], d["created_at"], d["status"],
                 d["payload"]))
            inserted += cur.rowcount
        for e in data["events"]:
            payload = e["payload"]
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                (e["seq"], e["event_id"], e["branch_id"], e["kind"],
                 e["actor"], e["at"], payload))
            inserted += cur.rowcount
        for b in data["branches"]:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO branches VALUES(?,?,?,?)",
                (b["branch_id"], b["parent"], b["created_seq"], b["label"]))
            inserted += cur.rowcount
        target_seq = max(self.current_seq(), data["counters"]["seq"])
        self.conn.execute("UPDATE counters SET value=? WHERE name='seq'",
                          (target_seq,))
        self.conn.commit()
        if not was_empty and inserted:
            self.add_event("import", {"inserted_rows": inserted,
                                      "source_seq": data["counters"]["seq"]},
                           actor=actor)
        return {"inserted_rows": inserted, "seq": self.current_seq()}
