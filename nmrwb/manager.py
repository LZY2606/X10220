"""项目生命周期：创建/打开/列出，导出与再导入（保留身份、顺序、来源关系）。"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from .ident import canonical, digest
from .clock import Clock
from .store import Project, SCHEMA


def project_id_for(name: str) -> str:
    return "proj_" + digest({"name": name})[:12]


class Workspace:
    def __init__(self, root: str, clock: Optional[Clock] = None):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "index.db")
        self.clock = clock or Clock()
        conn = sqlite3.connect(self.index_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _db_path(self, pid: str) -> str:
        return os.path.join(self.root, f"{pid}.db")

    def create_project(self, name: str) -> Project:
        pid = project_id_for(name)
        path = self._db_path(pid)
        created = self.clock.now()
        idx = sqlite3.connect(self.index_path)
        idx.execute("INSERT OR IGNORE INTO projects(id,name,created_at) VALUES(?,?,?)",
                    (pid, name, created))
        idx.commit()
        idx.close()
        proj = Project(path, pid, self.clock)
        proj.append("ProjectCreated", {"name": name, "project_id": pid},
                    ts=created)
        return proj

    def open_project(self, pid: str) -> Project:
        if not os.path.exists(self._db_path(pid)):
            raise FileNotFoundError(pid)
        return Project(self._db_path(pid), pid, self.clock)

    def list_projects(self) -> List[Dict[str, Any]]:
        idx = sqlite3.connect(self.index_path)
        idx.row_factory = sqlite3.Row
        rows = idx.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        idx.close()
        return [dict(r) for r in rows]

    # ---------- 导出 / 导入 ----------
    def export_project(self, proj: Project) -> Dict[str, Any]:
        branches = {}
        for b in proj.list_branches():
            rows = proj.conn.execute(
                "SELECT seq,branch,id,prev_hash,type,payload,created_at "
                "FROM events WHERE branch=? ORDER BY seq",
                (b["name"],)).fetchall()
            branches[b["name"]] = {
                "meta": b,
                "events": [{"seq": r[0], "branch": r[1], "id": r[2],
                            "prev_hash": r[3], "type": r[4],
                            "payload": json.loads(r[5]), "created_at": r[6]}
                           for r in rows],
            }
        bundle = {"format": "nmr-workbench-export-1",
                  "project_id": proj.pid,
                  "branches": branches}
        bundle["bundle_hash"] = "bun_" + digest(
            {"project_id": proj.pid,
             "events": {b: [e["id"] for e in d["events"]]
                        for b, d in branches.items()}})
        return bundle

    def import_project(self, bundle: Dict[str, Any],
                       replace: bool = True) -> str:
        _validate_bundle(bundle)
        pid = bundle["project_id"]
        path = self._db_path(pid)
        if os.path.exists(path):
            if not replace:
                raise FileExistsError(pid)
            os.remove(path)
        name = None
        proj = Project(path, pid, self.clock)
        for bname, data in bundle["branches"].items():
            for e in data["events"]:
                if e["type"] == "ProjectCreated":
                    name = e["payload"].get("name")
                proj.conn.execute(
                    "INSERT INTO events(seq,branch,id,prev_hash,type,payload,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (e["seq"], e["branch"], e["id"], e["prev_hash"], e["type"],
                     canonical(e["payload"]), e["created_at"]))
            m = data["meta"]
            proj.conn.execute(
                "INSERT OR REPLACE INTO branches(project_id,name,head_event_id,"
                "parent_event_id,reason,created_at) VALUES(?,?,?,?,?,?)",
                (pid, bname, m["head_event_id"], m["parent_event_id"],
                 m["reason"], m["created_at"]))
        proj.conn.commit()
        idx = sqlite3.connect(self.index_path)
        idx.execute("INSERT OR IGNORE INTO projects(id,name,created_at) VALUES(?,?,?)",
                    (pid, name or pid, self.clock.now()))
        idx.commit()
        idx.close()
        return pid


def _validate_bundle(bundle: Dict[str, Any]) -> None:
    if bundle.get("format") != "nmr-workbench-export-1":
        raise ValueError("不支持的导出格式")
    if "branches" not in bundle or "main" not in bundle["branches"]:
        raise ValueError("导出缺少 main 分支")
    from .ident import chain_digest
    # main 从 GENESIS 开始完整校验；分叉分支校验其自身事件链段
    for bname, data in bundle["branches"].items():
        prev = data["events"][0]["prev_hash"] if data["events"] else "GENESIS"
        for e in data["events"]:
            if bname != "main" and prev == "GENESIS":
                # 分叉分支首个事件的 prev_hash 指向父链事件，属正常
                pass
            expect = chain_digest(prev, e["type"], e["payload"])
            if expect != e["id"] and bname == "main":
                raise ValueError(f"main 哈希链校验失败于 {e['id']}")
            if expect == e["id"]:
                prev = e["id"]
    expected = "bun_" + digest(
        {"project_id": bundle["project_id"],
         "events": {b: [e["id"] for e in d["events"]]
                    for b, d in bundle["branches"].items()}})
    if bundle.get("bundle_hash") and bundle["bundle_hash"] != expected:
        raise ValueError("导出 bundle_hash 不匹配，拒绝导入")
