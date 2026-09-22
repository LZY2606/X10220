"use strict";
const state = {
  project: null, branch: "main", upto: null, view: null, events: null,
};

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status} ${t.slice(0, 300)}`);
  }
  return r.json();
};
const post = (path, body) => api(path, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body),
});
function toast(msg, isErr) {
  const el = $("status");
  el.textContent = msg;
  el.style.color = isErr ? "#ffb3ab" : "#cfe3ff";
}

async function loadProjects(keep) {
  const data = await api("/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = "";
  for (const p of data.projects) {
    const o = document.createElement("option");
    o.value = p.project_id; o.textContent = `${p.name} (${p.project_id})`;
    sel.appendChild(o);
  }
  if (state.project && data.projects.some(p => p.project_id === state.project)) {
    sel.value = state.project;
  } else if (!keep && sel.options.length) {
    state.project = sel.value;
  }
  if (state.project) sel.value = state.project;
}

async function refreshAll() {
  if (!state.project) return;
  const q = new URLSearchParams({branch: state.branch});
  if (state.upto !== null && state.upto !== undefined) q.set("upto_seq", state.upto);
  state.view = await api(`/api/projects/${state.project}/view?${q}`);
  for (const w of ["molecule", "peaks", "edges", "candidates"]) {
    const html = await (await fetch(
      `/api/projects/${state.project}/svg/${w}?${q}`)).text();
    $(w).innerHTML = html;
  }
  state.events = await api(`/api/projects/${state.project}/events`);
  renderBranches(); renderEvents(); renderConflicts(); bindMarks();
  toast(`当前视图：分支 ${state.branch} @ seq ${state.view.seq}` +
        (state.upto !== null && state.upto !== undefined ? "（历史重放）" : "（head）"));
}

function renderBranches() {
  const sel = $("branchSelect");
  const branches = Object.keys(state.events.branches).sort();
  if (!branches.includes(state.branch)) state.branch = "main";
  sel.innerHTML = "";
  branches.forEach(b => {
    const o = document.createElement("option");
    o.value = b; o.textContent = b;
    sel.appendChild(o);
  });
  sel.value = state.branch;
  const evs = state.events.branches[state.branch] || [];
  const maxSeq = evs.length ? evs[evs.length - 1].seq : 0;
  $("seqSlider").max = maxSeq;
  $("seqSlider").value = state.upto ?? maxSeq;
  $("seqLabel").textContent = (state.upto ?? maxSeq) + (state.upto == null ? " / head" : "");
}

function renderEvents() {
  const box = $("eventLog");
  box.innerHTML = "";
  const evs = state.events.branches[state.branch] || [];
  evs.forEach(e => {
    const d = document.createElement("div");
    d.className = "ev";
    const detail = JSON.stringify(e.payload).slice(0, 150);
    d.innerHTML = `<span class="k">#${e.seq} ${e.kind}</span> ` +
      `<span class="dim">${new Date(e.ts * 1000).toISOString()} ${e.author}</span><br>` +
      `<span class="dim">${detail}</span>`;
    d.title = e.event_id;
    d.onclick = () => { state.upto = e.seq; refreshAll(); };
    box.appendChild(d);
  });
}

function renderConflicts() {
  const cs = state.view.conflicts || [];
  const b = $("conflictBanner");
  if (!cs.length) { b.classList.add("hidden"); b.innerHTML = ""; return; }
  b.classList.remove("hidden");
  b.innerHTML = cs.map(c => {
    const btn = c.resolved ? "（已解决）" :
      `<button data-resolve='${JSON.stringify(c)}'>采纳 main 决定</button>`;
    return `核 ${c.nucleus_id}：main ${JSON.stringify(c.main_verdict)} ` +
      `(${c.authors[0]}) vs 分支 ${c.branch} ${JSON.stringify(c.branch_verdict)} ` +
      `(${c.authors[1]}) ${btn}`;
  }).join("<br>");
  b.querySelectorAll("button").forEach(btn => {
    btn.onclick = async () => {
      const c = JSON.parse(btn.dataset.resolve);
      const [verdict, pid] = c.main_verdict;
      await post(`/api/projects/${state.project}/resolve-conflict`, {
        nucleus_id: c.nucleus_id, verdict, peak_id: pid,
        winner_event: c.main_event, loser_event: c.branch_event,
      });
      state.upto = null; await refreshAll();
    };
  });
}

function bindMarks() {
  const link = (el) => {
    el.onclick = () => showProvenance(el.dataset.nid, el.dataset.pid, el.dataset.eid);
  };
  document.querySelectorAll("[data-nid]").forEach(link);
  document.querySelectorAll("tr[data-pid]").forEach(el => {
    el.onclick = () => showProvenance(el.dataset.nid || null, el.dataset.pid, null);
  });
  document.querySelectorAll("[data-eid]").forEach(el => {
    el.onclick = () => showProvenance(null, null, el.dataset.eid);
  });
  document.querySelectorAll("button[data-act]").forEach(btn => {
    btn.onclick = async () => {
      const verdict = btn.dataset.act === "accept" ? "accepted" : "rejected";
      try {
        const r = await post(`/api/projects/${state.project}/decisions`, {
          nucleus_id: btn.dataset.nid, verdict, peak_id: btn.dataset.pid,
          branch: state.branch,
        });
        toast(r.forked ? `冲突已保留为新分支 ${r.branch}` : `已记录 ${verdict}`);
        state.branch = r.branch; state.upto = null; await refreshAll();
      } catch (e) { toast(e.message, true); }
    };
  });
  highlightLinks();
}

function highlightLinks() {
  document.querySelectorAll(".mark").forEach(n => n.classList.remove("mark"));
  // candidates already rely on hover; cross-view linking via provenance clicks
}

function showProvenance(nid, pid, eid) {
  const v = state.view, a = v.analysis;
  const out = {project: state.project, branch: state.branch, seq: v.seq,
               algorithm_version: v.algorithm_version};
  if (pid) {
    for (const nuc of Object.keys(a.peaks)) {
      const p = a.peaks[nuc].find(x => x.id === pid);
      if (p) { out.peak = p; out.source_documents = v.document_versions; break; }
    }
  }
  if (eid) {
    for (const exp of Object.keys(a.observed_edges)) {
      const e = a.observed_edges[exp].find(x => x.edge_id === eid);
      if (e) { out.edge = e; break; }
    }
  }
  if (nid) {
    out.nucleus_candidates = a.candidates[nid] || null;
    out.decision = v.decisions[nid] || null;
    out.skeleton_atom = a.skeleton.atoms.find(x => x.id === nid) || null;
  }
  out.parameter_fingerprint = a.parameter_fingerprint;
  out.condition_fingerprint = a.condition_fingerprint;
  out.sources = a.sources;
  $("provenance").textContent = JSON.stringify(out, null, 2);
}

// --- toolbar wiring ------------------------------------------------------
$("projectSelect").onchange = async () => {
  state.project = $("projectSelect").value; state.branch = "main"; state.upto = null;
  await refreshAll();
};
$("branchSelect").onchange = async () => {
  state.branch = $("branchSelect").value; state.upto = null; await refreshAll();
};
$("seqSlider").oninput = async () => {
  const max = Number($("seqSlider").max);
  const val = Number($("seqSlider").value);
  state.upto = val >= max ? null : val;
  await refreshAll();
};
$("headBtn").onclick = async () => { state.upto = null; await refreshAll(); };
$("refreshBtn").onclick = refreshAll;
$("newProjectBtn").onclick = async () => {
  const pid = "p-" + Math.random().toString(36).slice(2, 8);
  const name = prompt("项目名称", "NMR review");
  if (!name) return;
  await post("/api/projects", {project_id: pid, name});
  state.project = pid; state.branch = "main"; state.upto = null;
  await loadProjects(); await refreshAll();
};
$("loadCaseBtn").onclick = async () => {
  try {
    const r = await post("/api/cases/load", {});
    state.project = r.project_id; state.branch = "main"; state.upto = null;
    await loadProjects(true); $("projectSelect").value = r.project_id;
    await refreshAll();
    toast("案例包已载入：基线/参数修订/偏置/两位编辑者冲突/条件变更均可重放");
  } catch (e) { toast(e.message, true); }
};
$("submitSourceBtn").onclick = async () => {
  try {
    const r = await post(`/api/projects/${state.project}/sources`, {
      logical_id: $("logicalId").value.trim(),
      filename: $("fileName").value.trim() || "pasted.nmr",
      raw_text: $("sourceText").value,
    });
    const diags = r.document.diagnostics;
    const box = $("sourceDiagnostics");
    box.classList.remove("hidden");
    box.textContent = (r.document.accepted ? "ACCEPTED\n" : "REJECTED\n") +
      JSON.stringify(diags, null, 2);
    toast(r.document.accepted ? "来源已作为不可变事件提交" :
                              "格式错误：来源被拒绝，原文已保留");
    await refreshAll();
  } catch (e) { toast(e.message, true); }
};
$("paramBtn").onclick = async () => {
  await post(`/api/projects/${state.project}/parameters`, {
    parameters: {shift_tolerance: {
      "1H": Number($("tolH").value), "13C": Number($("tolC").value)}},
    reason: $("reason").value,
  });
  state.upto = null; await refreshAll();
};
$("exportBtn").onclick = async () => {
  const b = await api(`/api/projects/${state.project}/export`);
  const blob = new Blob([JSON.stringify(b, null, 2)], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${state.project}-export.json`; a.click();
};
$("importFile").onchange = async () => {
  const f = $("importFile").files[0];
  if (!f) return;
  const bundle = JSON.parse(await f.text());
  try {
    const r = await post("/api/import-bundle", bundle);
    toast(`导入成功 ${r.imported}：${r.events} 个事件，身份与顺序校验通过`);
    state.project = r.imported; await loadProjects(true);
    $("projectSelect").value = r.imported; await refreshAll();
  } catch (e) { toast("导入失败：" + e.message, true); }
};
$("showFormatBtn").onclick = () => {
  $("sourceText").value =
`@document h1
@kind 1d
@version 1
@nucleus 1H
@solvent CDCl3
@temperature 298 K
@reference TMS 0.0 ppm
@spectrometer_frequency 400.13 MHz
@columns id shift intensity integral multiplicity j_hz
peak Hp1 1.234 ppm 1000 a.u. 3.0 H-equiv d 7.1 Hz
# 2D: @kind 2d  @experiment HSQC  edge E1 2.10 ppm 30.1 ppm 300 a.u.
# 骨架: @kind skeleton;  atom A C 13C predicted 30.0 ppm at 60 60;  bond b1 A B 1
# 明确错误：@kind 写错或缺少 @nucleus 会被拒绝；无法转换的单位保留并附诊断。`;
};

(async function init() {
  await loadProjects();
  if (!state.project && $("projectSelect").options.length) {
    state.project = $("projectSelect").value;
  }
  if (state.project) await refreshAll();
  else toast("请新建项目或载入仓库内离线案例包");
})();
