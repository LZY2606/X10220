"""SQLite 存储：不可变事件日志 + 物化状态 + 派生结果依赖表。

核心约束：
- 原始文档只追加新版本，永不覆盖（document_versions 唯一）。
- 派生结果以内容身份去重，记录其直接依赖的记录与条件签名。
- 事件构成父子链（分支）；冲突解决创建新分支并保留被否定分支。
- 全部身份显式存储，导出/删除/再导入后保持。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    lineage_id TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS document_versions (
    doc_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    project_id TEXT NOT NULL,
    format TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    author TEXT NOT NULL,
    change_note TEXT NOT NULL,
    has_error INTEGER NOT NULL DEFAULT 0,
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (doc_id, version)
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    label TEXT,
    lineno INTEGER NOT NULL,
    kind TEXT NOT NULL,
    raw TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS params_versions (
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    params_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    author TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (project_id, version)
);
CREATE TABLE IF NOT EXISTS results (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    nucleus TEXT NOT NULL,
    algo_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    conditions_signature TEXT NOT NULL,
    params_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS result_inputs (
    result_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (result_id, record_id, role)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    parent_id TEXT,
    branch TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    undone_by TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    peak_id TEXT,
    result_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    actor TEXT NOT NULL,
    rationale TEXT,
    supersedes_decision TEXT,
    conflict_id TEXT,
    adopted INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    status TEXT NOT NULL,
    resolution_event_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_findings (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    event_id TEXT,
    code TEXT NOT NULL,
    scope TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (id)
);
"""


def connect(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
