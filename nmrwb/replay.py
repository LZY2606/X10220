"""从事件链重放可复核状态。

不缓存任何“当前真值”到数据库：dataset、snapshot 状态、核归属、冲突、
影响集合都由事件序列确定性重建；回到任意旧事件得到当时版本与结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ident import digest
from .parser import parse_peak_list, ParsedDocument
from .model import build_dataset, Dataset
from .params import normalize_params, params_fingerprint
from .store import context_fp

SHIFT_CONTEXT_FIELDS = ("solvent", "temperature_k", "reference_standard")


@dataclass
class ViewState:
    branch: str
    at_event_id: Optional[str]
    events: List[Dict[str, Any]]
    sources: List[Dict[str, Any]]
    dataset: Optional[Dataset]
    params: Dict[str, Any]
    params_fingerprint: str
    context: Dict[str, Any]
    context_fingerprint: str
    inputs_hash: Optional[str]
    snapshots: List[Dict[str, Any]]
    decisions: List[Dict[str, Any]]
    assignments: Dict[str, Dict[str, Any]]
    nucleus_state: Dict[str, Dict[str, Any]]
    conflict_events: List[Dict[str, Any]]
    branch_meta: List[Dict[str, Any]]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch, "at_event_id": self.at_event_id,
            "events": self.events,
            "sources": self.sources,
            "dataset": self.dataset.to_dict() if self.dataset else None,
            "params": self.params, "params_fingerprint": self.params_fingerprint,
            "context": self.context, "context_fingerprint": self.context_fingerprint,
            "inputs_hash": self.inputs_hash,
            "snapshots": self.snapshots, "decisions": self.decisions,
            "assignments": self.assignments, "nucleus_state": self.nucleus_state,
            "conflict_events": self.conflict_events,
            "branch_meta": self.branch_meta, "warnings": self.warnings,
        }


def compute_inputs_hash(source_records: List[Dict[str, Any]]) -> str:
    latest: Dict[str, str] = {}
    for rec in source_records:
        latest[rec["slot"]] = rec["source_id"]
    return "in_" + digest({"sources": dict(sorted(latest.items()))})[:14]


def replay(project, branch: str = "main",
           upto_event_id: Optional[str] = None) -> ViewState:
    events = project.events(branch, upto_event_id)
    _verify_chain(events)

    source_records: List[Dict[str, Any]] = []
    parsed: Dict[str, ParsedDocument] = {}
    params = normalize_params({})
    decisions: List[Dict[str, Any]] = []
    snapshot_events: List[Dict[str, Any]] = []
    conflict_events: List[Dict[str, Any]] = []
    context_overrides: List[Dict[str, Any]] = []

    for ev in events:
        p = ev["payload"]
        t = ev["type"]
        if t == "SourceSubmitted":
            source_records.append(p)
        elif t == "ParametersSet":
            params = normalize_params(p["params"])
        elif t in ("DecisionMade", "DecisionReverted"):
            decisions.append({"event_id": ev["id"], "created_at": ev["created_at"],
                              **p})
        elif t == "SnapshotComputed":
            snapshot_events.append(ev)
        elif t == "ConflictResolved":
            conflict_events.append(ev)
        elif t == "ContextChanged":
            context_overrides.append(p)

    dataset = None
    if source_records:
        for rec in source_records:
            if rec["source_id"] not in parsed:
                parsed[rec["source_id"]] = parse_peak_list(rec["raw_text"])
        dataset = build_dataset(source_records, parsed)
        for ov in context_overrides:
            _apply_context_override(dataset.context, ov)

    ctx = dataset.context if dataset else {"solvent": None, "temperature_k": None,
                                           "reference_standard": None}
    cfp = context_fp(ctx)
    pfp = params_fingerprint(params)
    inputs_hash = compute_inputs_hash(source_records) if source_records else None

    snapshots = _classify_snapshots(snapshot_events, inputs_hash, pfp, cfp)
    assignments, nucleus_state = _fold_decisions(
        decisions, snapshots, events)

    return ViewState(
        branch=branch, at_event_id=upto_event_id, events=events,
        sources=_sources_view(source_records, parsed),
        dataset=dataset, params=params, params_fingerprint=pfp, context=ctx,
        context_fingerprint=cfp, inputs_hash=inputs_hash, snapshots=snapshots,
        decisions=decisions, assignments=assignments,
        nucleus_state=nucleus_state, conflict_events=conflict_events,
        branch_meta=project.list_branches(),
        warnings=(dataset.warnings if dataset else []))


def _verify_chain(events: List[Dict[str, Any]]) -> None:
    from .ident import chain_digest
    prev = "GENESIS"
    for ev in events:
        expect = chain_digest(prev, ev["type"], ev["payload"])
        if expect != ev["id"]:
            raise ValueError(f"事件哈希链校验失败于 {ev['id']}")
        prev = ev["id"]


def _sources_view(records: List[Dict[str, Any]],
                  parsed: Dict[str, ParsedDocument]) -> List[Dict[str, Any]]:
    out = []
    latest_slot: Dict[str, str] = {}
    for i, rec in enumerate(records):
        latest_slot[rec["slot"]] = rec["source_id"]
        doc = parsed.get(rec["source_id"])
        diags = [d.to_dict() for d in doc.diagnostics] if doc else []
        out.append({
            "id": rec["source_id"], "slot": rec["slot"],
            "version": rec["version"], "parent_id": rec.get("parent_id"),
            "raw_text": rec["raw_text"],
            "format": doc.format if doc else None,
            "meta": doc.meta if doc else {},
            "diagnostics": diags,
            "submitted_at": rec["submitted_at"],
            "is_current": False,
        })
    for item in out:
        item["is_current"] = latest_slot.get(item["slot"]) == item["id"]
    return out


def _apply_context_override(context: Dict[str, Any], ov: Dict[str, Any]) -> None:
    changes = ov.get("changes", {})
    if "solvent" in changes:
        context["solvent"] = changes["solvent"]
    if "reference_standard" in changes:
        context["reference_standard"] = changes["reference_standard"]
    if "temperature_k" in changes:
        val = changes["temperature_k"]
        context["temperature_k"] = ({"raw": str(val), "value": val, "unit": "K"}
                                    if isinstance(val, (int, float)) else val)
    if "spectrometer_frequency_hz" in changes:
        context.setdefault("spectrometer_frequency_hz", {}).update(
            changes["spectrometer_frequency_hz"])


def _classify_snapshots(snapshot_events: List[Dict[str, Any]],
                        inputs_hash: Optional[str], pfp: str,
                        cfp: str) -> List[Dict[str, Any]]:
    ordered = sorted(snapshot_events,
                     key=lambda e: (e["payload"]["seq"], e["created_at"]))
    matched = [ev["payload"]["snapshot"]["id"] for ev in ordered
               if (ev["payload"]["snapshot"]["inputs_hash"] == inputs_hash
                   and ev["payload"]["snapshot"]["params_fingerprint"] == pfp
                   and ev["payload"]["snapshot"]["context_fingerprint"] == cfp)]
    current_id = matched[-1] if matched else None
    out: List[Dict[str, Any]] = []
    for ev in ordered:
        snap = dict(ev["payload"]["snapshot"])
        snap["event_id"] = ev["id"]
        snap["computed_at"] = ev["created_at"]
        same = (snap["inputs_hash"] == inputs_hash
                and snap["params_fingerprint"] == pfp
                and snap["context_fingerprint"] == cfp)
        if same:
            snap["status"] = "current" if snap["id"] == current_id else "superseded"
        else:
            reasons = []
            if snap["inputs_hash"] != inputs_hash:
                reasons.append("SOURCE_CHANGED")
            if snap["params_fingerprint"] != pfp:
                reasons.append("PARAMS_CHANGED")
            if snap["context_fingerprint"] != cfp:
                reasons.append("CONTEXT_CHANGED")
            snap["status"] = "stale"
            snap["stale_reasons"] = reasons
        out.append(snap)
    return out


def _sources_view(records: List[Dict[str, Any]],
                  parsed: Dict[str, ParsedDocument]) -> List[Dict[str, Any]]:
    out = []
    latest_slot: Dict[str, str] = {}
    for i, rec in enumerate(records):
        latest_slot[rec["slot"]] = rec["source_id"]
        doc = parsed.get(rec["source_id"])
        diags = [d.to_dict() for d in doc.diagnostics] if doc else []
        out.append({
            "id": rec["source_id"], "slot": rec["slot"],
            "version": rec["version"], "parent_id": rec.get("parent_id"),
            "raw_text": rec["raw_text"],
            "format": doc.format if doc else None,
            "meta": doc.meta if doc else {},
            "diagnostics": diags,
            "submitted_at": rec["submitted_at"],
            "is_current": False,
        })
    for item in out:
        item["is_current"] = latest_slot.get(item["slot"]) == item["id"]
    return out


def _apply_context_override(context: Dict[str, Any], ov: Dict[str, Any]) -> None:
    changes = ov.get("changes", {})
    if "solvent" in changes:
        context["solvent"] = changes["solvent"]
    if "reference_standard" in changes:
        context["reference_standard"] = changes["reference_standard"]
    if "temperature_k" in changes:
        val = changes["temperature_k"]
        context["temperature_k"] = ({"raw": str(val), "value": val, "unit": "K"}
                                    if isinstance(val, (int, float)) else val)
    if "spectrometer_frequency_hz" in changes:
        context.setdefault("spectrometer_frequency_hz", {}).update(
            changes["spectrometer_frequency_hz"])


def _classify_snapshots(snapshot_events: List[Dict[str, Any]],
                        inputs_hash: Optional[str], pfp: str,
                        cfp: str) -> List[Dict[str, Any]]:
    ordered = sorted(snapshot_events, key=lambda e: (e["payload"]["seq"],
                                                     e["created_at"]))
    latest_ids = []
    seen_keys = set()
    for ev in ordered:
        snap = ev["payload"]["snapshot"]
        same = (snap["inputs_hash"] == inputs_hash and
                snap["params_fingerprint"] == pfp and
                snap["context_fingerprint"] == cfp)
        snap = dict(snap)
        snap["event_id"] = ev["id"]
        snap["computed_at"] = ev["created_at"]
        if same:
            snap["status"] = "current"
            latest_ids.append(snap["id"])
        else:
            reasons = []
            if snap["inputs_hash"] != inputs_hash:
                reasons.append("SOURCE_CHANGED")
            if snap["params_fingerprint"] != pfp:
                reasons.append("PARAMS_CHANGED")
            if snap["context_fingerprint"] != cfp:
                reasons.append("CONTEXT_CHANGED")
            snap["status"] = "stale"
            snap["stale_reasons"] = reasons
        yield_view = snap
    # 只允许最新一个匹配快照为 current（同键多次重算时）
    current_id = latest_ids[-1] if latest_ids else None
    out = []
    for ev in ordered:
        snap = dict(ev["payload"]["snapshot"])
        snap["event_id"] = ev["id"]
        snap["computed_at"] = ev["created_at"]
        same = (snap["inputs_hash"] == inputs_hash and
                snap["params_fingerprint"] == pfp and
                snap["context_fingerprint"] == cfp)
        if same:
            snap["status"] = "current" if snap["id"] == current_id else "superseded"
        else:
            reasons = []
            if snap["inputs_hash"] != inputs_hash:
                reasons.append("SOURCE_CHANGED")
            if snap["params_fingerprint"] != pfp:
                reasons.append("PARAMS_CHANGED")
            if snap["context_fingerprint"] != cfp:
                reasons.append("CONTEXT_CHANGED")
            snap["status"] = "stale"
            snap["stale_reasons"] = reasons
        out.append(snap)
    return out


def _fold_decisions(decisions: List[Dict[str, Any]],
                    snapshots: List[Dict[str, Any]],
                    events: List[Dict[str, Any]]) -> (Dict[str, Any], Dict[str, Any]):
    """按事件顺序折叠人工决定，得到每个核当前的归属与状态。"""
    assignments: Dict[str, Dict[str, Any]] = {}
    nucleus_state: Dict[str, Dict[str, Any]] = {}
    event_time = {e["id"]: e["created_at"] for e in events}

    for d in decisions:
        if d["type"] == "DecisionReverted":
            target = d.get("revert_event_id")
            # 撤销：移除该决定及其之后对同目标的覆盖，恢复到目标之前的可见决定
            assignments = {k: v for k, v in assignments.items()
                           if v.get("decision_event_id") != target}
            continue
        action = d.get("action")
        editor = d.get("editor", "anonymous")
        for nucleus in d.get("nuclei", []):
            prev = assignments.get(nucleus)
            if prev and prev.get("editor") != editor and prev.get("action") != action:
                # 冲突不覆盖：保留两位编辑者的冲突，等待解决事件
                nucleus_state.setdefault(nucleus, {})
                nucleus_state[nucleus]["conflict"] = {
                    "status": "unresolved",
                    "editors": [prev["editor"], editor],
                    "events": [prev["decision_event_id"], d["event_id"]],
                    "actions": [prev["action"], action]}
            assignments[nucleus] = {
                "nucleus": nucleus,
                "action": action,
                "candidate_id": d.get("candidate_id"),
                "candidate_ids": d.get("candidate_ids"),
                "ambiguity_group": d.get("ambiguity_group"),
                "editor": editor,
                "note": d.get("note"),
                "decision_event_id": d["event_id"],
                "decided_at": d["created_at"],
                "based_on_snapshot": d.get("based_on_snapshot"),
                "supersedes": prev["decision_event_id"] if prev else None,
            }
            st = nucleus_state.setdefault(nucleus, {})
            st["decision"] = assignments[nucleus]
            if st.get("conflict"):
                # 新决定不抹除冲突标记，交由 ConflictResolved 处理
                pass

    # 冲突解决事件
    for e in events:
        if e["type"] != "ConflictResolved":
            continue
        p = e["payload"]
        for nucleus in p.get("nuclei", []):
            st = nucleus_state.setdefault(nucleus, {})
            st["conflict"] = {
                "status": "resolved",
                "resolution_event_id": e["id"],
                "winning_editor": p.get("winning_editor"),
                "winning_event_id": p.get("winning_event_id"),
                "note": p.get("note")}

    _annotate_current_basis(assignments, snapshots)
    return assignments, nucleus_state


def _annotate_current_basis(assignments: Dict[str, Any],
                            snapshots: List[Dict[str, Any]]) -> None:
    current = next((s for s in reversed(snapshots)
                    if s["status"] == "current"), None)
    cur_ids = {c["id"] for c in current["candidates"]} if current else set()
    for nuc, a in assignments.items():
        cid = a.get("candidate_id")
        if current is None:
            a["basis_status"] = "no_current_snapshot"
        elif cid and cid not in cur_ids:
            a["basis_status"] = "stale_basis"
            a["replayable"] = True
        else:
            a["basis_status"] = "current_basis"
