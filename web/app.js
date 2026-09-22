"use strict";
let VIEW = null;
let SEL = {kind:"peak", iso:"1H", id:null};

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.status + " " + r.statusText;
    try { const j = await r.json(); msg = j.detail ? JSON.stringify(j.detail) : msg; }
    catch (e) {}
    throw new Error(msg);
  }
  return r.status === 200 ? r.json() : {};
};

function toast(msg, bad) {
  const box = document.getElementById("toast");
  const d = document.createElement("div");
  d.className = "toast"; d.textContent = msg;
  if (bad) d.style.borderColor = "var(--bad)";
  box.appendChild(d);
  setTimeout(() => d.remove(), 5200);
}

const pid = () => document.getElementById("pid").value.trim();

async function createProject() {
  await api("/api/projects", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({project_id: pid(), name:"NMR review"})});
  toast("项目已创建");
  await load();
}

async function loadCase() {
  try {
    const out = await api(`/api/projects/${pid()}/load-case?actor=reviewer`,
                          {method:"POST"});
    toast("已载入 " + out.events.length + " 个来源版本事件");
  } catch (e) { toast("载入失败：" + e.message, true); }
  await load();
}

async function exportBundle() {
  const b = await api(`/api/projects/${pid()}/export`);
  const blob = new Blob([JSON.stringify(b, null, 2)],
                        {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${pid()}-bundle.json`;
  a.click();
  toast("已导出 bundle（含稳定身份与顺序）");
}

async function importBundle(file) {
  if (!file) return;
  const text = await file.text();
  const r = await fetch("/api/import-bundle",
    {method:"POST", headers:{"Content-Type":"application/json"}, body:text});
  if (!r.ok) { toast("导入失败 " + r.status, true); return; }
  const j = await r.json();
  document.getElementById("pid").value = j.project_id;
  toast("导入成功，身份/顺序保持：" + j.project_id);
  await load();
}

async function load() {
  const branch = document.getElementById("branch").value || "B:main";
  const atEvent = document.getElementById("atEvent").value || "";
  let q = `?branch=${encodeURIComponent(branch)}`;
  if (atEvent) q += `&at_event=${encodeURIComponent(atEvent)}`;
  try {
    VIEW = await api(`/api/projects/${pid()}/view${q}`);
  } catch (e) {
    document.getElementById("sources").innerHTML =
      `<span class="muted">项目不存在或为空：${e.message}</span>`;
    return;
  }
  fillBranchSelect();
  render();
}

function fillBranchSelect() {
  const bs = document.getElementById("branch");
  const cur = bs.value || "B:main";
  bs.innerHTML = "";
  VIEW.branches.forEach(b => {
    const o = document.createElement("option");
    o.value = b.branch_id; o.textContent = b.label + "  (" + b.branch_id + ")";
    bs.appendChild(o);
  });
  if ([...bs.options].some(o => o.value === cur)) bs.value = cur;

  const es = document.getElementById("atEvent");
  const curE = es.value;
  es.innerHTML = '<option value="">(最新)</option>';
  VIEW.events.forEach(e => {
    const o = document.createElement("option");
    o.value = e.event_id;
    o.textContent = `${e.seq} ${e.kind} @${e.actor}`;
    es.appendChild(o);
  });
  if (curE) es.value = curE;
}

function render() {
  renderSources();
  renderHistory();
  renderJudgements();
  renderStats();
  renderPeakSvg();
  renderSkeleton();
  renderCandidates();
}

function sourceList() {
  return Object.entries(VIEW.parsed).map(([sid, d]) => ({sid, ...d}));
}

function renderSources() {
  const box = document.getElementById("sources");
  box.innerHTML = "";
  sourceList().forEach(s => {
    const div = document.createElement("div");
    div.className = "item";
    const nPeaks = (s.data.peaks || []).length;
    const nEdges = (s.data.edges || []).length;
    const meta = s.meta || {};
    div.innerHTML =
      `<div><b>${s.sid}</b> <span class="tag">${s.kind}</span>` +
      `<span class="tag">${s.ref}</span></div>` +
      `<div class="muted">${nPeaks} peaks ${nEdges} edges · ` +
      `${meta.solvent||""} ${meta.frequency||""}</div>` +
      `<div class="muted">${meta.temperature||""} ref ${meta.reference||""}</div>`;
    div.onclick = () => showRaw(s.sid);
    box.appendChild(div);
  });
}

async function showRaw(sid) {
  const vid = VIEW.active_versions[sid];
  const j = await api(`/api/projects/${pid()}/sources/${sid}/${vid}`);
  document.getElementById("rawBox").textContent =
    `# ${sid}/${vid}  content_hash=${j.content_hash}\n` + j.raw_text;
}

function renderHistory() {
  const box = document.getElementById("history");
  box.innerHTML = "";
  VIEW.events.forEach(e => {
    const div = document.createElement("div");
    div.className = "item";
    let detail = "";
    const p = e.payload || {};
    if (p.candidate_id) detail = p.candidate_id;
    if (p.source_id) detail += " " + p.source_id + "@" + (p.version_id||"");
    if (p.run_id) detail += " run=" + p.run_id.slice(0,14);
    div.innerHTML = `<span class="tag">${e.event_id}</span>` +
      `<b>${e.kind}</b> <span class="muted">@${e.actor} · ${detail}</span>`;
    box.appendChild(div);
  });
}

function renderJudgements() {
  const box = document.getElementById("judgements");
  const run = VIEW.run;
  box.innerHTML = "";
  const add = (code, sev, msg, basis) => {
    const d = document.createElement("div");
    d.className = "judgement " + (sev || "info");
    d.innerHTML = `<span class="tag ${sev==="warning"?"warn":"ok"}">${code}</span>`+
      `<span class="muted">${msg||""}</span>` +
      (basis?`<pre style="max-height:90px">${JSON.stringify(basis,null,1)}</pre>`:"");
    box.appendChild(d);
  };
  if (run && run.judgements) run.judgements.forEach(j =>
    add(j.code, j.severity, j.message, j.basis));
  VIEW.events.filter(e => e.kind === "decision_conflict" ||
    e.kind === "conflict_resolved" || e.kind === "candidate_expired")
    .forEach(e => add(e.kind, e.kind==="decision_conflict"?"warning":"info",
      (e.payload||{}).message, e.payload));
}

function renderStats() {
  const box = document.getElementById("stats");
  if (!VIEW.run) { box.innerHTML = '<span class="muted">尚无计算结果。</span>'; return; }
  const r = VIEW.run;
  const bias = r.global_bias || {};
  box.innerHTML =
    `<div>algo <span class="tag">${r.algo_version}</span>`+
    `pf <span class="tag">${r.param_fingerprint.slice(0,10)}</span></div>`+
    `<div class="muted">inputs ${r.inputs_fingerprint.slice(0,10)} · run ${r.run_id}</div>`+
    `<div style="margin-top:6px">` +
    Object.entries(r.statistics).map(([k,v])=>
      `<span class="tag">${k}:${v}</span>`).join("") + `</div>` +
    `<div style="margin-top:6px">整体偏置: ` +
    (bias.detected
      ? `<span class="tag warn">${bias.offset_ppm} ppm</span>`
      : `<span class="tag ok">未检出</span>`) + `</div>` +
    (r.stale ? `<div class="tag bad">此 run 已过期（仅历史可读）</div>` : "");
}

function peaksOf(iso) {
  let out = [];
  sourceList().forEach(s => {
    if (s.kind === (iso === "1H" ? "1h" : "13c")) {
      (s.data.peaks||[]).forEach(p => out.push({...p, ref:s.ref}));
    }
  });
  return out;
}

function edgesOf() {
  const out = [];
  sourceList().forEach(s => {
    if (["cosy","hsqc","hmbc"].includes(s.kind)) {
      (s.data.edges||[]).forEach(e => out.push({...e, ref:s.ref, exp:s.kind}));
    }
  });
  return out;
}

function renderPeakSvg() {
  const iso = document.getElementById("isoSel").value;
  SEL.iso = iso;
  const svg = document.getElementById("peakSvg");
  const peaks = peaksOf(iso);
  svg.innerHTML = "";
  if (!peaks.length) {
    svg.innerHTML = '<text x="8" y="20" fill="#93a0b8">该核种无 1D 峰来源</text>';
    return;
  }
  const W = svg.clientWidth || 600, H = 170, pad = 44;
  const shifts = peaks.map(p => p.shift.value).filter(v=>v!=null);
  let lo = Math.min(...shifts), hi = Math.max(...shifts);
  if (hi - lo < 0.5) { lo -= 0.3; hi += 0.3; }
  const x = v => pad + (hi - v) / (hi - lo) * (W - 2*pad);
  const ns = "http://www.w3.org/2000/svg";
  const line = document.createElementNS(ns,"line");
  line.setAttribute("x1",pad); line.setAttribute("x2",W-pad);
  line.setAttribute("y1",120); line.setAttribute("y2",120);
  line.setAttribute("stroke","#28324a"); svg.appendChild(line);
  peaks.forEach(p => {
    const v = p.shift.value;
    const cx = x(v), h = 20 + (p.intensity.value||0.5)*80;
    const g = document.createElementNS(ns,"g");
    g.setAttribute("class","peak-g"); g.style.cursor="pointer";
    const rect = document.createElementNS(ns,"rect");
    rect.setAttribute("x",cx-7); rect.setAttribute("y",120-h);
    rect.setAttribute("width",14); rect.setAttribute("height",h);
    rect.setAttribute("rx",3);
    rect.setAttribute("fill", SEL.id===p.id ? "#5db0ff":"#3b5a8c");
    const t = document.createElementNS(ns,"text");
    t.setAttribute("x",cx); t.setAttribute("y",138);
    t.setAttribute("text-anchor","middle"); t.setAttribute("fill","#cdd7ea");
    t.textContent = `${p.pid} ${v==null?"?":v}`;
    const it = document.createElementNS(ns,"text");
    it.setAttribute("x",cx); it.setAttribute("y",154);
    it.setAttribute("text-anchor","middle"); it.setAttribute("fill","#93a0b8");
    it.textContent = `∫${p.integral.raw} ${p.multiplicity}`;
    g.appendChild(rect); g.appendChild(t); g.appendChild(it);
    g.onclick = () => { SEL={kind:"peak", iso, id:p.id}; render(); };
    svg.appendChild(g);
  });
  document.getElementById("selInfo").textContent =
    `${peaks.length} peaks · 单位 ppm（基准 ${peaks[0].shift.basis||"delta"}），强度 a.u.`;
}

function findPeak(peakId) {
  for (const iso of ["1H","13C"]) {
    const p = peaksOf(iso).find(p => p.id === peakId);
    if (p) return p;
  }
  return null;
}

function renderSkeleton() {
  const svg = document.getElementById("skelSvg");
  svg.innerHTML = "";
  const ns = "http://www.w3.org/2000/svg";
  if (!VIEW.skeleton) {
    svg.innerHTML='<text x="8" y="20" fill="#93a0b8">缺少骨架来源</text>'; return;
  }
  const sh = VIEW.skeleton.shapes;
  const atoms = sh.atoms;
  const W = svg.clientWidth || 600, H = 330;
  const [minx,miny,maxx,maxy] = sh.bounds;
  const pad = 46;
  const sx = x => pad + (x-minx)/((maxx-minx)||1)*(W-2*pad);
  const sy = y => pad + (y-miny)/((maxy-miny)||1)*(H-2*pad);
  const pos = {};
  atoms.forEach(a => pos[a.id] = [sx(a.x), sy(a.y)]);

  // Highlight edges incident to the selected peak.
  const selPeak = SEL.kind==="peak" ? SEL.id.split("/P:")[1] : null;
  edgesOf().forEach(e => {
    if (!selPeak) return;
    const [i1,p1] = e.dim1.split(":"); const [i2,p2] = e.dim2.split(":");
    let from=null, to=null, color=null;
    if (p1 === selPeak) { to = atomByPeak(i2,p2); from = atomByPeak(i1,p1); color=e.exp; }
    if (p2 === selPeak) { to = atomByPeak(i1,p1); from = atomByPeak(i2,p2); color=e.exp; }
    if (from && to && pos[from] && pos[to]) {
      const l = document.createElementNS(ns,"line");
      l.setAttribute("x1",pos[from][0]); l.setAttribute("y1",pos[from][1]);
      l.setAttribute("x2",pos[to][0]); l.setAttribute("y2",pos[to][1]);
      l.setAttribute("stroke", color==="hsqc"?"#4cc38a":color==="cosy"?"#5db0ff":"#e0a94d");
      l.setAttribute("stroke-dasharray", color==="hmbc"?"6 4":"2 2");
      l.setAttribute("stroke-width","2");
      svg.appendChild(l);
    }
  });

  sh.bonds.forEach(b => {
    if (!pos[b.a] || !pos[b.b]) return;
    const l = document.createElementNS(ns,"line");
    l.setAttribute("x1",pos[b.a][0]); l.setAttribute("y1",pos[b.a][1]);
    l.setAttribute("x2",pos[b.b][0]); l.setAttribute("y2",pos[b.b][1]);
    l.setAttribute("stroke","#44557a");
    l.setAttribute("stroke-width", b.order===2?2.5:1.4);
    svg.appendChild(l);
  });
  atoms.forEach(a => {
    if (a.isotope !== SEL.iso) return;
    const [cx,cy] = pos[a.id];
    const g = document.createElementNS(ns,"g"); g.style.cursor="pointer";
    const c = document.createElementNS(ns,"circle");
    c.setAttribute("cx",cx); c.setAttribute("cy",cy); c.setAttribute("r",15);
    c.setAttribute("fill", SEL.kind==="nucleus" && SEL.id===a.id ? "#5db0ff":"#223252");
    c.setAttribute("stroke", a.equiv_size>1 ? "#e0a94d":"#3b5a8c");
    c.setAttribute("stroke-width","2");
    const t = document.createElementNS(ns,"text");
    t.setAttribute("x",cx); t.setAttribute("y",cy+4);
    t.setAttribute("text-anchor","middle"); t.setAttribute("fill","#e8edf6");
    t.textContent = a.id;
    const eq = document.createElementNS(ns,"text");
    eq.setAttribute("x",cx+14); eq.setAttribute("y",cy-12);
    eq.setAttribute("fill","#e0a94d");
    eq.textContent = a.equiv_size>1 ? `×${a.equiv_size}` : "";
    g.appendChild(c); g.appendChild(t); g.appendChild(eq);
    g.onclick = () => { SEL={kind:"nucleus", iso:a.isotope, id:a.id}; render(); };
    svg.appendChild(g);
  });
}

function atomByPeak(iso, pid) {
  if (!VIEW.run) return null;
  for (const n of VIEW.run.nuclei) {
    const hit = n.candidates.find(c => c.peak_pid === pid && c.isotope === iso
      && !c.rejected && c.rank === 1);
    if (hit) return n.nucleus;
  }
  return null;
}

function renderCandidates() {
  const box = document.getElementById("cands");
  if (!VIEW.run) { box.innerHTML='<span class="muted">尚无 run。</span>'; return; }
  let nucleus = null;
  if (SEL.kind === "nucleus") nucleus = SEL.id;
  if (SEL.kind === "peak") {
    const pk = findPeak(SEL.id);
    if (pk) SEL.iso = pk.pid; // keep
  }
  const rows = VIEW.run.nuclei.filter(n =>
    (n.isotope === SEL.iso) &&
    (nucleus ? n.nucleus === nucleus
             : SEL.kind==="peak" && n.candidates.some(c => c.peak_id===SEL.id)));

  box.innerHTML = "";
  if (!rows.length) {
    box.innerHTML = '<span class="muted">没有与当前选中联动的候选。</span>';
    return;
  }
  rows.forEach(n => {
    const h = document.createElement("div");
    h.innerHTML = `<h2 style="margin:6px 0">核 ${n.nucleus} `+
      `<span class="tag">${n.state}</span>`+
      `<span class="tag">sym ${n.symmetry_class}</span></h2>`+
      (n.note?`<div class="tag warn">${n.note}</div>`:"");
    box.appendChild(h);
    const shown = SEL.kind==="peak"
      ? n.candidates.filter(c => c.peak_id===SEL.id)
      : n.candidates;
    shown.forEach(c => box.appendChild(candEl(c, n)));
  });
}

function candEl(c, n) {
  const d = document.createElement("div");
  d.className = "cand " + (c.rejected?"rej":c.rank===1?"top":"") +
    (c.tie_with && c.tie_with.length ? " tied":"");
  const dec = (VIEW.decisions_by_candidate[c.id]||[]).slice(-1)[0];
  const decTag = dec ? `<span class="tag ${
      dec.kind==="candidate_accepted"?"ok":"bad"}">${
      dec.kind==="candidate_accepted"?"已接受":"已否定"} @${dec.actor}</span>` : "";
  d.innerHTML =
    `<div><b>${c.peak_pid}</b> ${c.shift_ppm==null?"?":c.shift_ppm+"ppm"} `+
    `<span class="tag">rank ${c.rank}</span>`+
    `<span class="tag">score ${c.score.toFixed(3)}</span>`+
    (c.tie_with&&c.tie_with.length?
      `<span class="tag warn">并列 ${c.tie_with.length}</span>`:"")+decTag+
    ` <span class="tag">${c.id}</span></div>`+
    `<div class="muted">Δδ ${c.shift_delta} ppm · 先验 ${c.expected_ppm} ppm`+
    (c.rejected?` · <span class="tag bad">${c.rejection.code}</span>`:"")+`</div>`+
    (c.evidence.length?`<div class="muted">`+
      c.evidence.map(e=>`<span class="tag ${e.supports?"ok":"bad"}">${e.code}</span>`).join("")+
      `</div>`:"");
  const acts = document.createElement("div");
  acts.className = "row";
  const a = document.createElement("button");
  a.className="accept"; a.textContent="接受(新分支)";
  a.onclick = () => decide(c.id,"accept");
  const r = document.createElement("button");
  r.className="reject"; r.textContent="否定(新分支)";
  r.onclick = () => decide(c.id,"reject");
  acts.appendChild(a); acts.appendChild(r);
  d.appendChild(acts);
  const why = document.createElement("details");
  why.innerHTML = `<summary>证据 / 拒绝原因 / 来源</summary>`+
    `<pre>${JSON.stringify({evidence:c.evidence,rejection:c.rejection,
      depends_on:c.depends_on, param_fingerprint:VIEW.run.param_fingerprint,
      run_id:VIEW.run.run_id}, null, 1)}</pre>`;
  d.appendChild(why);
  return d;
}

async function decide(candidateId, action) {
  const actor = prompt("审阅者标识：", action==="accept"?"alice":"bob");
  if (!actor) return;
  const reason = prompt("理由（可空）：", "") || "";
  const branch = document.getElementById("branch").value || "B:main";
  const base = VIEW.events.length ? VIEW.events[VIEW.events.length-1].event_id : null;
  try {
    const out = await api(`/api/projects/${pid()}/decisions`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({candidate_id:candidateId, action, actor, reason,
        branch_id:branch, expected_base_event_id:base,
        create_child_branch:true})});
    document.getElementById("branch").value = out.branch_id;
    toast(`${action} 已写入新分支 ${out.branch_id}`+
      (out.conflict_event_id?"；检测到两编辑者冲突":""));
  } catch (e) { toast("裁定失败：" + e.message, true); }
  await load();
}
