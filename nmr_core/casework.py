"""离线案例包「NMR 共振经纬：化学位移、耦合候选与归属证据室」。

叙事主线：结构化学小组把 1H / 13C 一维峰表与 COSY/HSQC/HMBC 二维相关边
合到同一个化合物（4-羟基苯甲酸甲酯）分析中，为每个核建立可复核的归属候选。

脚本依次经过：基线 -> 参数修订 -> 局部源改动 -> 整体位移偏置 ->
两编辑者冲突 -> 冲突解决 -> 撤销，覆盖全部验收边界情形。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .project import ProjectError, ProjectService, project_id_for

CASE_DIR = Path(__file__).parent / "casepack"

CASE_NAME = "NMR 共振经纬：化学位移、耦合候选与归属证据室"
CASE_NARRATIVE = (
    "结构化学小组把一维（1H/13C）与二维（COSY/HSQC/HMBC）NMR 谱图的 peak list "
    "合到同一个化合物分析中，为每个核建立可复核的归属候选。"
)

PARAMS_V1 = {
    "solvent": "CDCl3",
    "temperature_K": 298.0,
    "reference": "TMS",
    "frequency_mhz": 400.13,
    "shift_tolerance_ppm": 0.06,
    "c13_tolerance_ppm": 0.5,
    "tie_epsilon": 0.001,
    "overlap_margin_ppm": 0.03,
    "weights": {"shift": 1.0, "integration": 0.5, "multiplicity": 0.3,
                "hsqc": 0.6, "cosy": 0.4, "hmbc": 0.4},
}

PARAMS_V2 = dict(PARAMS_V1, solvent="DMSO-d6", temperature_K=310.0)


def _read(name: str) -> str:
    return (CASE_DIR / name).read_text(encoding="utf-8")


def load_case_pack(service: ProjectService, actor: str = "casework") -> dict[str, Any]:
    """执行案例脚本，返回项目 id 与关键节点（供测试与界面反查）。"""
    try:
        pid = service.create_project(CASE_NAME, CASE_NARRATIVE, actor=actor)
    except ProjectError as e:
        if e.code == "PROJECT_EXISTS":
            return {"project": project_id_for(CASE_NAME), "already_loaded": True}
        raise

    log: list[dict[str, Any]] = []

    # 1) 基线：原始文件按版本保留（化学位移/峰强/积分/多重性/相关边/溶剂/仪器频率）
    for key, fname, note in [
        ("skeleton", "skeleton_v1.txt", "基线：骨架与期望位移"),
        ("h1", "h1_v1.txt", "基线：1H 峰表"),
        ("c13", "c13_v1.txt", "基线：13C 峰表"),
        ("corr", "corr_v1.txt", "基线：COSY/HSQC/HMBC 相关边"),
    ]:
        r = service.commit_source(pid, key, _read(fname), actor=actor, note=note)
        log.append({"step": f"commit {key} v1", "version": r["version_id"]})

    r = service.commit_params(pid, PARAMS_V1, actor=actor, note="基线参数：CDCl3 / 298K / TMS / 400.13 MHz")
    log.append({"step": "params v1", "version": r["version_id"]})

    base = service.compute(pid, actor=actor)
    log.append({"step": "compute baseline", "set_id": base["set_id"]})
    base_event = base["events"][0]["event_id"]

    # 2) 两个编辑者的冲突决定（同一基准事件、不同裁定）+ 一条否定决定
    d1 = service.decide(pid, "chen", "cd-tg-1H-A8-h1:H04", base["set_id"], "confirm",
                        rationale="HSQC/HMBC 证据支持 3.86 ppm 峰归属 OCH3",
                        judgment_basis=["COUPLING_EVIDENCE_ONLY"],
                        base_event_id=base_event)
    d2 = service.decide(pid, "watanabe", "cd-tg-1H-A8-h1:H05", base["set_id"], "confirm",
                        rationale="并列最优中偏好 3.94 ppm 峰（积分复测一致）",
                        judgment_basis=["COUPLING_EVIDENCE_ONLY"],
                        base_event_id=base_event)
    conflict_id = d2["impact"]["conflicts"][0] if d2["impact"]["conflicts"] else None
    d3 = service.decide(pid, "chen", "cd-tg-1H-ring-b-h1:H03", base["set_id"], "reject",
                        rationale="重叠峰不强制唯一匹配；暂否决缺 HSQC 支持的一侧，保留复测",
                        judgment_basis=["OVERLAP_NOT_FORCED"])
    log.append({"step": "conflicting decisions",
                "decisions": [d1["decision_id"], d2["decision_id"], d3["decision_id"]],
                "conflict": conflict_id})

    # 3) 参数修订：溶剂/温度变更只使相关候选过期，旧决定仍可重放
    r = service.commit_params(pid, PARAMS_V2, actor="watanabe",
                              note="参数修订：溶剂改 DMSO-d6、温度 310K，仅使依赖候选过期")
    log.append({"step": "params v2", "version": r["version_id"],
                "staled": r["impact"]["newly_stale_sets"]})
    cs2 = service.compute(pid, actor=actor)
    log.append({"step": "compute after params v2", "set_id": cs2["set_id"]})

    # 4) 整体化学位移偏置：1H 源 v2（+0.05 ppm），只使依赖它的候选过期
    r = service.commit_source(pid, "h1", _read("h1_v2_biased.txt"), actor=actor,
                              note="重新定标：整体偏置 +0.05 ppm")
    log.append({"step": "commit h1 v2 (biased)", "version": r["version_id"],
                "staled": r["impact"]["newly_stale_sets"]})
    cs3 = service.compute(pid, actor=actor)
    log.append({"step": "compute after bias", "set_id": cs3["set_id"]})

    # 5) 局部源改动：13C v2 修正 C04、删除伪峰 C05（ring-b 碳目标变为缺峰）
    r = service.commit_source(pid, "c13", _read("c13_v2_fix.txt"), actor=actor,
                              note="局部修正：C04 位移订正；C05 复核为溶剂伪峰删除")
    log.append({"step": "commit c13 v2 (local fix)", "version": r["version_id"],
                "staled": r["impact"]["newly_stale_sets"]})
    cs4 = service.compute(pid, actor=actor)
    log.append({"step": "compute after local fix", "set_id": cs4["set_id"]})

    # 6) 冲突解决：采纳 watanabe 的决定；被否决的决定仍保留在档案中
    if conflict_id:
        service.resolve_conflict(pid, conflict_id, d2["decision_id"], actor="chen",
                                 rationale="复测积分支持 H05；旧决定仍可重放，不覆盖历史")
    # 7) 撤销写入历史：chen 撤销自己对 H03 的否定
    service.revert_decision(pid, d3["decision_id"], actor="chen",
                            reason="撤销：复测后恢复 H03 候选资格，重叠峰保持双候选")
    log.append({"step": "conflict resolved + revert"})

    return {
        "project": pid,
        "already_loaded": False,
        "log": log,
        "baseline_set": base["set_id"],
        "final_set": cs4["set_id"],
        "conflict_id": conflict_id,
        "decisions": {"confirm_h04": d1["decision_id"],
                      "confirm_h05": d2["decision_id"],
                      "reject_h03": d3["decision_id"]},
    }
