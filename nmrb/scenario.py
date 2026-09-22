"""离线案例运行器：以确定性时间重放完整叙事。

流程（单条连贯故事线 + 复核分支）：
1. 基线导入（原始文件与版本保留，含无法转换值诊断）。
2. 参数修订（收紧容差）。
3. 只影响局部依赖的源数据改动（H_G / C_G）。
4. 缺峰修订（只新增 G 位点 unknown 候选）。
5. 参考标准变更（整体偏置；仅 1H 候选条件过期，13C 仍 current）。
6. 并列最优（E/F）与重叠/对称位点的人工复核，两个编辑者冲突并解决。
7. 撤销也写入历史。
"""
from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Dict, List, Optional

from .assign import DEFAULT_PARAMS
from .service import ProjectService
from .storage import connect
from .timeutil import Clock
from .repository import Repo

CASE_DIR = pathlib.Path(__file__).parent / "case"
BASE_FILES = [
    "exemplin-a.skel",
    "exemplin-a-1h.v1.pk1",
    "exemplin-a-13c.v1.pk1",
    "exemplin-a-cosy.v1.peak2",
    "exemplin-a-hsqc.v1.peak2",
    "exemplin-a-hmbc.v1.peak2",
]

FIXED_EVENTS = [
    "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00",
    "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00",
    "2026-01-01T00:04:00+00:00", "2026-01-01T00:05:00+00:00",
]


class ScriptedClock(Clock):
    def __init__(self, timestamps: List[str]):
        super().__init__()
        self._stamps = list(timestamps)
        self._i = 0

    def now(self) -> str:
        if self._i < len(self._stamps):
            ts = self._stamps[self._i]
            self._i += 1
            return ts
        return f"2026-01-02T00:{self._i - len(self._stamps):02d}:00+00:00"


def _read(name: str) -> str:
    return (CASE_DIR / name).read_text(encoding="utf-8")


def load_case(db_path: str = ":memory:", project_id: str = "case-exemplin-a",
              run_scripted: bool = True, start_at: str = FIXED_EVENTS[0],
              existing_repo: Optional[Repo] = None
              ) -> Dict[str, Any]:
    if existing_repo is not None:
        # 用脚本化时钟临时驱动既有连接，完成后恢复调用方时钟。
        conn = existing_repo.conn
        original_clock = existing_repo.clock
        repo = existing_repo
        repo.clock = ScriptedClock([_bump(start_at, i) for i in range(400)])
        svc = ProjectService(repo)
        def _restore_clock() -> None:
            repo.clock = original_clock
    else:
        conn = connect(db_path)
        repo = Repo(conn, ScriptedClock([_bump(start_at, i) for i in range(400)]))
        svc = ProjectService(repo)
        def _restore_clock() -> None:
            return None
    repo.create_project(project_id, "Exemplin-A：NMR 共振经纬证据室")
    parent: Optional[str] = None
    trace: List[Dict[str, Any]] = []

    # 1) 基线导入
    for fname in BASE_FILES:
        out = svc.import_source(project_id, fname, _read(fname), "importer",
                                f"基线导入 {fname}", parent)
        parent = out["event"]["id"]
        trace.append({"step": "import", "file": fname, "event": parent,
                      "diagnostics": out["diagnostics"]})
        for diag in out["diagnostics"]:
            repo.add_scenario_finding(project_id, diag["code"], fname,
                                      diag, parent)
    if not run_scripted:
        _restore_clock()
        return {"service": svc, "repo": repo, "conn": conn,
                "project_id": project_id, "trace": trace}

    # 2) 参数修订
    p2 = copy.deepcopy(DEFAULT_PARAMS)
    p2["shift_tolerance_ppm"]["1H"] = 0.18
    p2["weights"]["shift"] = 0.34
    p2["weights"]["hsqc"] = 0.26
    rev = svc.revise_params(project_id, p2, "spectroscopist-li",
                            "收紧 1H 位移容差并重平衡 HSQC 权重", parent)
    parent = rev["event"]["id"]
    trace.append({"step": "params_revised", "event": parent})

    # 3) 局部源数据改动（1H + 13C 各一次）
    local_h = svc.import_source(project_id, "exemplin-a-1h.v1.pk1",
                                _read("exemplin-a-1h.v2-local.pk1"), "importer",
                                "局部改动：H_G 1.28->1.20 ppm", parent)
    parent = local_h["event"]["id"]
    trace.append({"step": "local_change_1h", "event": parent})
    local_c = svc.import_source(project_id, "exemplin-a-13c.v2-local.pk1",
                                _read("exemplin-a-13c.v2-local.pk1"), "importer",
                                "局部改动：C_G 15.4->15.0 ppm", parent)
    parent = local_c["event"]["id"]
    trace.append({"step": "local_change_13c", "event": parent})

    # 4) 缺峰（仅 G）
    missing = svc.import_source(project_id, "exemplin-a-1h.v1.pk1",
                                _read("exemplin-a-1h.v3-missing.pk1"), "importer",
                                "缺峰实验：删除 H_G 行", parent)
    parent = missing["event"]["id"]
    trace.append({"step": "missing_peak", "event": parent})
    _record_phenomenon(svc, repo, project_id, parent)

    # 5) 参考标准变更（整体偏置）
    ref = svc.change_conditions(project_id, "exemplin-a-1h.v1.pk1",
                                "reference",
                                _read("exemplin-a-1h.v4-reference.pk1"),
                                "spectroscopist-wang", parent)
    parent = ref["event"]["id"]
    trace.append({"step": "reference_change", "event": parent})
    _record_phenomenon(svc, repo, project_id, parent)

    # 6) 回到“最新输入”恢复 v1 1H 以便人工复核（作为新版本，不覆盖历史）
    restore = svc.import_source(project_id, "exemplin-a-1h.v1.pk1",
                                _read("exemplin-a-1h.v1.pk1"), "importer",
                                "复核前恢复参考标准（新版本，历史仍可重放）", parent)
    parent = restore["event"]["id"]
    trace.append({"step": "reference_restored", "event": parent})

    # 7) 两个编辑者对并列最优 E/F 分别作出冲突决定
    snap = svc.snapshot(project_id, parent)
    h_result = next(r for r in snap["results"] if r["nucleus"] == "1H" and r["state"] == "current")
    e_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_E")
    f_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_F")
    e_peaks = [c["peak_id"] for c in h_result["candidate_map"][e_site]
               if c["kind"] == "assignment"]
    f_peaks = [c["peak_id"] for c in h_result["candidate_map"][f_site]
               if c["kind"] == "assignment"]
    dec_li = svc.decide(project_id, e_site, e_peaks[0], h_result["id"],
                        "accept", "editor-li", "李：依据 HSQC 35.6 采纳第一个并列峰",
                        parent, branch="review-li")
    dec_wang = svc.decide(project_id, e_site, e_peaks[-1], h_result["id"],
                          "accept", "editor-wang", "王：依据积分形态采纳另一并列峰",
                          parent, branch="review-wang")
    trace.append({"step": "conflicting_decisions",
                  "li": dec_li["decision"]["decision_id"],
                  "wang": dec_wang["decision"]["decision_id"]})
    cid = svc.detect_conflict(project_id, e_site,
                              dec_li["decision"]["decision_id"],
                              dec_wang["decision"]["decision_id"], parent)
    # 8) 冲突解决：采纳李，王的决定保留但不采纳
    resolve = svc.resolve(project_id, cid, dec_li["decision"]["decision_id"],
                          dec_wang["decision"]["decision_id"], "lead-zhao",
                          "裁决：HSQC 直连证据优先，王的分支保留为未采纳历史",
                          dec_li["decision"]["event"]["id"], "review-li")
    trace.append({"step": "conflict_resolved", "conflict_id": cid,
                  "event": resolve["resolution_event"]["id"]})

    # 9) 对重叠/对称位点做一次接受 + 一次撤销（撤销写历史）
    ab_site = next(s["id"] for s in snap["skeleton"]["sites"] if s["hydrogen"] == "H_A")
    ab_peak = h_result["candidate_map"][ab_site][0]["peak_id"]
    dec_ab = svc.decide(project_id, ab_site, ab_peak, h_result["id"], "accept",
                        "editor-li", "接受重叠对称峰的合法候选",
                        resolve["resolution_event"]["id"], branch="review-li")
    undo_parent = (dec_ab["recomputed_event"]["id"]
                   if dec_ab.get("recomputed_event")
                   else dec_ab["decision"]["event"]["id"])
    undo = svc.undo(project_id, dec_ab["decision"]["event"]["id"], "editor-li",
                    undo_parent, "review-li")
    trace.append({"step": "undo_recorded", "event": undo["undo_event"]["id"]})

    _record_phenomenon(svc, repo, project_id, parent)
    _restore_clock()
    return {"service": svc, "repo": repo, "conn": conn,
            "project_id": project_id, "trace": trace,
            "head": undo["undo_event"]["id"]}


def _bump(iso: str, minutes: int) -> str:
    from datetime import datetime, timedelta
    dt = datetime.fromisoformat(iso)
    return (dt + timedelta(minutes=minutes)).isoformat()


def _record_phenomenon(svc: ProjectService, repo: Repo, project_id: str,
                       parent: Optional[str]) -> None:
    snap = svc.snapshot(project_id, parent)
    codes = set()
    for res in snap["results"]:
        for f in res["findings"]:
            codes.add(f["code"])
            repo.add_scenario_finding(project_id, f["code"], f"{f['subject']}:{res['nucleus']}",
                                      {"finding": f, "result_id": res["id"],
                                       "result_state": res["state"]}, parent)
    return
