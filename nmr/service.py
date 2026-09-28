"""Application service: orchestrates the event log, parser and engine."""
from __future__ import annotations

from datetime import datetime, timezone

from . import chemistry
from .chemistry import DEFAULT_PARAMS
from .events import initial_state, make_event, reduce_event, replay, visible_event_ids
from .ident import content_sha, stable_id
from .parser import FormatError, parse_peaklist, parse_skeleton
from .storage import EventStore

CONDITION_KEYS = ("solvent", "temperature", "reference")


class Clock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class FixedClock:
    def __init__(self, timestamps: list[str]):
        self._timestamps = list(timestamps)
        self._i = 0

    def now(self) -> str:
        value = self._timestamps[min(self._i, len(self._timestamps) - 1)]
        self._i += 1
        return value


def role_for_content(filename: str, text: str) -> str:
    head = "\n".join(text.splitlines()[:5])
    if "%skeleton" in head:
        return "skeleton"
    if "%source" in head:
        return "peaklist"
    raise FormatError(["cannot determine file kind: need %skeleton or %source header"])


class ProjectService:
    def __init__(self, store: EventStore, clock: Clock | None = None):
        self.store = store
        self.clock = clock or Clock()

    # ------------------------------------------------------------------ state
    def state(self, project_id: str) -> dict:
        return replay(self.store.load_events(project_id))

    def _append(self, project_id: str, kind: str, payload: dict, *, branch: str,
                editor: str, parent_event_id: str | None,
                merge_parents: list[str] | None = None) -> dict:
        state = self.state(project_id)
        seq = self.store.next_seq(project_id)
        event = make_event(kind, payload, seq=seq, parent_event_id=parent_event_id,
                           branch=branch, timestamp=self.clock.now(), editor=editor,
                           merge_parents=merge_parents)
        reduce_event(state, event)
        self.store.append(project_id, event)
        return event

    # --------------------------------------------------------------- projects
    def create_project(self, title: str = "Untitled compound",
                       editor: str = "system") -> str:
        project_id = stable_id("proj", title, "v1", length=12)
        state = initial_state()
        event = make_event("project_created",
                           {"project_id": project_id, "title": title}, seq=1,
                           parent_event_id=None, branch="main",
                           timestamp=self.clock.now(), editor=editor)
        reduce_event(state, event)
        self.store.create_project(project_id, title, event["timestamp"])
        self.store.append(project_id, event)
        return project_id

    # ---------------------------------------------------------------- sources
    def submit_source(self, project_id: str, filename: str, raw_content: str, *,
                      version: str, branch: str = "main", editor: str = "importer",
                      note: str = "", explicit_role: str | None = None) -> dict:
        state = self.state(project_id)
        role = explicit_role or role_for_content(filename, raw_content)
        if role == "skeleton":
            parsed = parse_skeleton(filename, raw_content, f"{project_id}:skeleton:{version}")
            source_role = "skeleton"
        else:
            parsed = parse_peaklist(filename, raw_content,
                                    f"{project_id}:{version}:{content_sha(raw_content)[:8]}")
            source_role = parsed["nucleus"]
        chain = state["source_chains"].get(source_role, [])
        kind = "source_submitted" if not chain else "source_revised"
        parent_version = chain[-1]["version"] if chain else None
        if chain and version == parent_version:
            raise ValueError("raw inputs are immutable; submit a new version string")
        payload = {
            "role": source_role,
            "version": version,
            "parent_version": parent_version,
            "filename": filename,
            "raw_content": raw_content,
            "content_sha": content_sha(raw_content),
            "parsed": parsed,
            "submitted_by": editor,
            "note": note,
        }
        head = state["branch_heads"][branch]
        return self._append(project_id, kind, payload, branch=branch, editor=editor,
                            parent_event_id=head)

    # --------------------------------------------------------------- revisions
    def revise_params(self, project_id: str, params: dict, *, branch: str = "main",
                      editor: str = "reviewer", reason: str = "", note: str = "") -> dict:
        state = self.state(project_id)
        payload = {"params": params, "reason": reason, "note": note}
        return self._append(project_id, "params_revised", payload, branch=branch,
                            editor=editor, parent_event_id=state["branch_heads"][branch])

    def create_branch(self, project_id: str, name: str, *, from_branch: str = "main",
                      editor: str = "reviewer", note: str = "") -> dict:
        state = self.state(project_id)
        payload = {"name": name, "from_branch": from_branch, "note": note}
        return self._append(project_id, "branch_created", payload, branch=from_branch,
                            editor=editor, parent_event_id=state["branch_heads"][from_branch])

    # --------------------------------------------------------------- compute
    def compute_candidates(self, project_id: str, *, branch: str = "main",
                           editor: str = "engine", note: str = "",
                           params_override: dict | None = None) -> dict:
        state = self.state(project_id)
        branch_state = self._branch_state(project_id, branch)
        skeleton_record = branch_state["sources"].get("skeleton")
        if skeleton_record is None:
            raise ValueError("cannot compute: skeleton missing on this branch")
        parsed_sources = {}
        basis_versions = {"skeleton": skeleton_record["version"]}
        for nuc in ("1H", "13C"):
            record = branch_state["sources"].get(nuc)
            if record is None:
                raise ValueError(f"cannot compute: {nuc} peak list missing on this branch")
            parsed_sources[nuc] = record["parsed"]
            basis_versions[nuc] = record["version"]
        params = params_override or branch_state["params"] or DEFAULT_PARAMS
        run_index = len(branch_state["runs"]) + 1
        run = chemistry.run_candidates(skeleton_record["parsed"], parsed_sources,
                                       params, run_index=run_index,
                                       basis_versions=basis_versions)
        payload = {"run": run, "note": note}
        return self._append(project_id, "candidates_computed", payload, branch=branch,
                            editor=editor,
                            parent_event_id=state["branch_heads"][branch])

    # -------------------------------------------------------------- decisions
    def decide(self, project_id: str, *, site_id: str, action: str,
               candidate_key: str | None, run_id: str, branch: str,
               editor: str, note: str = "") -> dict:
        if action not in ("accept", "reject", "defer"):
            raise ValueError("action must be accept/reject/defer")
        state = self.state(project_id)
        branch_state = self._branch_state(project_id, branch)
        run = next((r for r in branch_state["runs"] if r["run_id"] == run_id), None)
        if run is None:
            raise ValueError(f"run {run_id} not visible on branch {branch!r}")
        site_result = next((r for r in run["site_results"] if r["site_id"] == site_id), None)
        if site_result is None:
            raise ValueError(f"site {site_id} not in run")
        if action in ("accept", "reject") and candidate_key is not None:
            valid = {c["peak_stable_id"] for c in site_result["candidates"]}
            if candidate_key not in valid:
                raise ValueError("candidate is not a legal candidate for the site in this run")
        payload = {
            "site_id": site_id,
            "action": action,
            "candidate_key": candidate_key,
            "run_id": run_id,
            "basis_versions": run["basis_versions"],
            "conditions_snapshot": run.get("conditions", {}),
            "params_fingerprint": run["params_fingerprint"],
            "note": note,
        }
        return self._append(project_id, "decision_made", payload, branch=branch,
                            editor=editor, parent_event_id=state["branch_heads"][branch])

    def resolve_conflict(self, project_id: str, *, site_id: str, resolution: str,
                         chosen_decision_id: str | None = None, editor: str = "moderator",
                         branches: list[str] | None = None, note: str = "") -> dict:
        if resolution not in ("choose", "keep_all", "reopen"):
            raise ValueError("resolution must be choose/keep_all/reopen")
        state = self.state(project_id)
        branches = branches or [b for b in state["branches"]]
        merge_parents = [state["branch_heads"][b] for b in branches
                         if b in state["branch_heads"]]
        payload = {
            "site_id": site_id,
            "resolution": resolution,
            "chosen_decision_id": chosen_decision_id,
            "branches": branches,
            "note": note,
        }
        return self._append(project_id, "conflict_resolved", payload, branch="main",
                            editor=editor,
                            parent_event_id=state["branch_heads"]["main"],
                            merge_parents=merge_parents)

    # ------------------------------------------------------------- branch util
    def _branch_state(self, project_id: str, branch: str) -> dict:
        state = self.state(project_id)
        ids = set(visible_event_ids(state, branch))
        branch_state = initial_state()
        branch_state["project_id"] = project_id
        for event in state["events"]:
            if event["id"] in ids:
                branch_state = reduce_event(branch_state, event)
        return branch_state
