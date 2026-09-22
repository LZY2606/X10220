"""共享的依赖装配（DB 路径可由环境变量覆盖，测试用临时目录）。"""
from __future__ import annotations

import os
from functools import lru_cache

from .repository import Repo
from .scenario import load_case
from .service import ProjectService
from .storage import connect
from .timeutil import Clock

DB_PATH = os.environ.get("NMRB_DB", os.path.join(os.getcwd(), "nmrb_workbench.db"))


class State:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = connect(db_path)
        self.clock = Clock()
        self.repo = Repo(self.conn, self.clock)
        self.service = ProjectService(self.repo)

    def ensure_demo(self) -> str:
        row = self.conn.execute(
            "SELECT id FROM projects WHERE id='case-exemplin-a'").fetchone()
        if row:
            return "case-exemplin-a"
        out = load_case(self.db_path, existing_repo=self.repo)
        return out["project_id"]


_state: State | None = None


def get_state() -> State:
    global _state
    if _state is None:
        _state = State(DB_PATH)
    return _state


def reset_state(db_path: str) -> State:
    global _state
    if _state is not None:
        try:
            _state.conn.close()
        except Exception:
            pass
    _state = State(db_path)
    return _state
