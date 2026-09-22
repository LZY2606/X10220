"use strict";
const state = {
  projectId: null, snap: null, atEvent: null,
  selectedSite: null, selectedPeak: null, selectedCandidate: null,
  nucleus: "1H", exportBundle: null
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const shortId = (id) => id ? String(id).slice(0, 10) : "—";

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function currentResults() {
  return state.snap.results.filter((r) => r.nucleus === state.nucleus);
}
function currentResult() {
  return currentResults().find((r) => r.state === "current")
    || currentResults()[0] || null;
}
function resultForEvent(resultId) {
  return state.snap.results.find((r) => r.id === resultId) || null;
}
function siteById(id) {
  return state.snap.skeleton.sites.find((s) => s.id === id) || null;
}
function peakById(id) {
  return state.snap.peaks.find((p) => p.id === id) || null;
}
function headEvent() {
  if (!state.snap) return null;
  return state.snap.events.find((e) => e.id === state.snap.head_event)
    || state.snap.events[state.snap.events.length - 1];
}

// ---------- 分子骨架 SVG ----------
function renderSkeleton() {
  const sites = state.snap.skeleton.sites;
  const expEdges = state.snap.skeleton.expected_edges;
  const hydroSites = sites.filter((s) => s.hydrogen !== "-");
  const carbonSites = sites.filter((s) => s.hydrogen === "-");
  const W = 430, rowH = 34, padX = 46, top = 30;
  const pos = {};
  hydroSites.forEach((s, i) => {
    pos[s.hydrogen] = {x: padX + (i % 5) * 82, y: top + Math.floor(i / 5) * 78 + 40, s, kind: "H"};
  });
  carbonSites.forEach((s, i) => {
    pos[s.carbon] = {x: padX + (i % 5) * 82, y: top + Math.floor(i / 5) * 78 + 8 + 40 + 78, s, kind: "C"};
  });
  let svg = `<svg viewBox="0 0 ${W} ${Math.max(220, 40 + Math.ceil(hydroSites.length / 5) * 78 + 110)}" width="100%">`;
  // C-H 连接
  hydroSites.forEach((s) => {
    const h = pos[s.hydrogen], c = pos[s.carbon];
    if (h && c) svg += `<line x1="${h.x}" y1="${h.y}" x2="${c.x}" y2="${c.y}" stroke="#4a5a78" stroke-width="1.4"/>`;
  });
  // 预期边（H-H 与 H-C HMBC），观察到=红，缺失=灰虚
  const observedPairs = observedEdgePairs();
  expEdges.forEach((ee) => {
    let a = pos[ee.a], b = pos[ee.b];
    if (!a) a = hydroSites.find((s) => s.hydrogen === ee.a) ? pos[ee.a] : null;
    if (!b) b = pos[ee.b];
    if (!a || !b) return;
    const found = observedPairs.has(`${ee.a}|${ee.b}`) || observedPairs.has(`${ee.b}|${ee.a}`);
    svg += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"
      stroke="${found ? "#e0856a" : "#6b7280"}" stroke-width="${found ? 2 : 1.2}"
      stroke-dasharray="${found ? "none" : "4 3"}" opacity="0.9"/>`;
  });
  Object.values(pos).forEach((p) => {
    const selected = state.selectedSite === p.s.id;
    const fill = p.kind === "H" ? "#2f78c8" : "#2f9e6a";
    const r = selected ? 11 : 8;
    svg += `<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${fill}"
      stroke="${selected ? "#fff" : "#0e1626"}" stroke-width="2"
      data-site="${p.s.id}" class="site-node" style="cursor:pointer"/>`;
    svg += `<text x="${p.x}" y="${p.y - r - 5}" text-anchor="middle" fill="#cdd8ee" font-size="9.5">${esc(p.kind === "H" ? p.s.hydrogen : p.s.carbon)}</text>`;
  });
  svg += `</svg>`;
  $("skeleton-svg").innerHTML = svg;
  document.querySelectorAll(".site-node").forEach((el) =>
    el.addEventListener("click", () => selectSite(el.dataset.site)));
}

function observedEdgePairs() {
  // 返回当前 1H 结果里 status=support 的 COSY/HMBC 端点对（用位点氢/碳标签）
  const set = new Set();
  const res = currentResult();
  if (!res) return set;
  const hByShift = new Map(state.snap.peaks
    .filter((p) => p.nucleus === "1H" && p.shift_ppm != null)
    .map((p) => [p.shift_ppm, p]));
  Object.entries(res.candidate_map).forEach(([sid, cands]) => {
    const site = siteById(sid);
    if (!site) return;
    cands.filter((c) => c.peak_id).slice(0, 1).forEach((c) => {
      // 用证据 source 边反推对端位点
      c.evidences.forEach((ev) => {
        if ((ev.code === "cosy" || ev.code === "hmbc") && ev.status === "support") {
          ev.source_record_ids.forEach((eid) => {
            const edge = state.snap.edges.find((e) => e.id === eid);
            if (!edge) return;
            const partnerShift = Math.abs(edge.f1_ppm - (peakById(c.peak_id)?.shift_ppm ?? -999)) < 0.05
              ? edge.f2_ppm : edge.f1_ppm;
            const psite = peakSiteAtShift(partnerShift, edge.experiment);
            if (psite) {
              if (edge.experiment === "COSY") set.add(`${site.hydrogen}|${psite.hydrogen}`);
              else set.add(`${site.hydrogen}|${psite.carbon}`);
            }
          });
        }
      });
    });
  });
  return set;
}

function peakSiteAtShift(shift, experiment) {
  // 通过 HSQC/最近峰找到该位移对应的位点（仅用于画观测边）
  if (shift == null) return null;
  const isCarbon = experiment === "HMBC" || shift > 50;
  const peaks = state.snap.peaks.filter((p) => (isCarbon ? p.nucleus === "13C" : p.nucleus === "1H"));
  let best = null, bestD = Infinity;
  peaks.forEach((p) => {
    if (p.shift_ppm == null) return;
    const d = Math.abs(p.shift_ppm - shift);
    if (d < bestD) { bestD = d; best = p; }
  });
  if (!best) return null;
  const res = state.snap.results.find((r) => r.nucleus === (isCarbon ? "13C" : "1H"));
  for (const [sid, cands] of Object.entries(res?.candidate_map || {})) {
    if (cands.some((c) => c.peak_id === best.id)) return siteById(sid);
  }
  return null;
}

// ---------- 谱峰棒图 ----------
function renderSpectrum() {
  const peaks = state.snap.peaks
    .filter((p) => p.nucleus === state.nucleus && p.shift_ppm != null)
    .sort((a, b) => b.shift_ppm - a.shift_ppm);
  const res = currentResult();
  const overlapIds = new Set();
  (res?.findings || []).filter((f) => f.code === "OVERLAP_PEAK")
    .forEach((f) => f.related.forEach((id) => overlapIds.add(id)));
  if (!peaks.length) { $("spectrum-svg").innerHTML = `<div class="empty">无可绘制位移（含无法转换值时保留在峰表）。</div>`; return; }
  const W = 460, H = 150, pad = 26;
  const min = Math.min(...peaks.map((p) => p.shift_ppm)) * 0.96;
  const max = Math.max(...peaks.map((p) => p.shift_ppm)) * 1.04 + 0.05;
  const x = (ppm) => pad + (max - ppm) / (max - min || 1) * (W - 2 * pad);
  const maxI = Math.max(...peaks.map((p) => p.intensity ?? 50), 1);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%">`;
  for (let g = 0; g <= 4; g++) {
    const xv = pad + g * (W - 2 * pad) / 4;
    const v = (max - g * (max - min) / 4).toFixed(state.nucleus === "1H" ? 2 : 1);
    svg += `<line x1="${xv}" y1="14" x2="${xv}" y2="${H - 20}" stroke="#24304a"/><text x="${xv}" y="${H - 6}" text-anchor="middle" fill="#8b98b2" font-size="9">${v} ppm</text>`;
  }
  peaks.forEach((p) => {
    const h = 18 + (p.intensity ?? maxI * 0.5) / maxI * (H - 52);
    const color = overlapIds.has(p.id) ? "#f2b441" : "#5aa7ff";
    const sel = state.selectedPeak === p.id;
    svg += `<line x1="${x(p.shift_ppm)}" y1="${H - 20}" x2="${x(p.shift_ppm)}" y2="${H - 20 - h}"
      stroke="${color}" stroke-width="${sel ? 4 : 2}" data-peak="${p.id}" style="cursor:pointer"/>`;
    const label = state.snap.skeleton.sites
      .flatMap((s) => []) ;
    svg += `<text x="${x(p.shift_ppm)}" y="${H - 24 - h}" text-anchor="middle" fill="#a9c4ea" font-size="8.5">${esc(p.label || "")}</text>`;
  });
  svg += `</svg>`;
  $("spectrum-svg").innerHTML = svg;
  document.querySelectorAll("[data-peak]").forEach((el) =>
    el.addEventListener("click", () => selectPeak(el.dataset.peak)));
}

// ---------- 峰表 ----------
function renderPeaksTable() {
  const peaks = state.snap.peaks
    .filter((p) => p.nucleus === state.nucleus)
    .sort((a, b) => (b.shift_ppm ?? -1) - (a.shift_ppm ?? -1));
  let html = `<table><thead><tr><th>标签</th><th>位移</th><th>峰强(原)</th><th>积分[单位]</th><th>多重性</th><th>条件</th><th>身份</th></tr></thead><tbody>`;
  peaks.forEach((p) => {
    const badShift = p.shift_ppm == null, badI = p.intensity == null;
    const sel = state.selectedPeak === p.id ? "selected" : "";
    html += `<tr class="row ${sel}" data-peak="${p.id}">
      <td>${esc(p.label)}</td>
      <td>${badShift ? `<span class="pill warn">原值 ${esc(p.shift_repr)} 无法转换</span>` : `${p.shift_ppm} ${p.shift_unit}`}</td>
      <td>${badI ? `<span class="pill warn">${esc(p.intensity_repr)} (留原位)</span>` : `${p.intensity} <small class="hint">${esc(p.intensity_unit)}</small>`}</td>
      <td>${p.integral ?? "—"} ${esc(p.integral_unit)}</td>
      <td>${esc(p.multiplicity)}</td>
      <td><small>${esc(p.conditions.solvent)} ${esc(p.conditions.reference)} ${p.conditions.spectrometer_mhz ?? "?"}MHz</small></td>
      <td><span class="idbadge" title="${esc(p.id)}">${shortId(p.id)}</span></td></tr>`;
  });
  html += `</tbody></table>`;
  $("peaks-table").innerHTML = html;
  document.querySelectorAll("#peaks-table [data-peak]").forEach((el) =>
    el.addEventListener("click", () => selectPeak(el.dataset.peak)));
}

// ---------- 相关边表 ----------
function renderEdgesTable() {
  const groups = {COSY: [], HSQC: [], HMBC: []};
  state.snap.edges.forEach((e) => (groups[e.experiment] ||= []).push(e));
  let html = "";
  for (const [exp, list] of Object.entries(groups)) {
    html += `<div class="hint">${exp}（${list.length}）</div><table><thead><tr><th>标签</th><th>f1 ppm</th><th>f2 ppm</th><th>强度</th><th>身份</th></tr></thead><tbody>`;
    list.forEach((e) => {
      const missing = (state.snap.scenario_findings || [])
        .some((f) => f.code === "MISSING_EXPECTED_EDGE" && JSON.stringify(f.payload).includes(e.id));
      html += `<tr class="edge-row ${missing ? "missing" : ""}">
        <td>${esc(e.label)}</td><td>${e.f1_ppm ?? esc(e.f1_repr)} ${esc(e.f1_unit)}</td>
        <td>${e.f2_ppm ?? esc(e.f2_repr)} ${esc(e.f2_unit)}</td>
        <td>${esc(e.strength_repr)}</td>
        <td><span class="idbadge" title="${esc(e.id)}">${shortId(e.id)}</span></td></tr>`;
    });
    html += `</tbody></table>`;
  }
  // 缺失预期边（观测中不存在的期望）
  const res = currentResult();
  const missing = (res?.findings || []).filter((f) => f.code === "MISSING_EXPECTED_EDGE");
  if (missing.length) {
    html += `<div class="hint">缺失相关边（只作证据，不据此唯一化）：</div>`;
    missing.forEach((f) => { html += `<div class="diag">· ${esc(f.subject)} — ${esc(f.message)}</div>`; });
  }
  $("edges-table").innerHTML = html || `<div class="empty">无二维边。</div>`;
}

// ---------- 候选面板 ----------
function renderCandidates() {
  const panel = $("candidate-panel");
  const subj = $("cand-subject");
  if (!state.selectedSite) { panel.innerHTML = `<div class="empty">点击骨架位点或峰行查看候选。</div>`; subj.textContent = ""; return; }
  const site = siteById(state.selectedSite);
  const res = currentResult();
  subj.textContent = `${site.hydrogen === "-" ? site.carbon : site.hydrogen} · 结果 ${shortId(res?.id)} · ${res?.state || "—"}`;
  const cands = (res?.candidate_map || {})[site.id];
  if (!cands) { panel.innerHTML = `<div class="empty">当前结果中无该位点候选。</div>`; return; }
  const findings = (res?.findings || []).filter((f) => f.subject === site.hydrogen || f.related?.includes(site.id));
  let html = findings.map((f) => `<span class="pill ${f.severity === "warning" ? "warn" : "info"}">${f.code}</span>`).join("");
  cands.forEach((c, idx) => {
    const peak = c.peak_id ? peakById(c.peak_id) : null;
    const sel = state.selectedCandidate === c.peak_id && idx === 0 ? "" : "";
    const cls = `cand ${c.rank === 1 ? "best" : ""} ${c.tied_rank ? "tied" : ""}`;
    const human = c.human ? `<span class="pill ${c.human.verdict}">人工:${c.human.verdict} by ${esc(c.human.actor)}</span>` : "";
    html += `<div class="${cls}" data-peak="${esc(c.peak_id || "")}">
      <div class="line">
        <b>#${c.rank}${c.tied_rank ? "（并列）" : ""}</b>
        ${peak ? `峰 ${esc(peak.label)} ${peak.shift_ppm} ppm` : `<span class="pill warn">unknown：缺峰/不唯一</span>`}
        <span class="scorebar"><i style="width:${Math.round(c.score * 100)}%"></i></span>
        <b>${c.score.toFixed(3)}</b>
        ${c.overlap ? `<span class="pill warn">重叠峰</span>` : ""}
        ${human}
        <span class="pill ${c.status === "confirmed" ? "accept" : c.status === "rejected" ? "reject" : "info"}">${c.status}</span>
      </div>
      <div class="evgrid">${c.evidences.map((e) => `
        <div class="ev ${e.status}"><b>${e.code} · ${e.status} · ${(e.weight * e.contribution).toFixed(2)}</b>${esc(e.detail)}
        <div class="sources">${e.source_record_ids.map(shortId).join(", ") || "无直接记录"}</div></div>`).join("")}
      </div>
      ${c.reject_reasons.length ? `<div class="diag">${c.reject_reasons.map(esc).join("；")}</div>` : ""}
      <div class="sources">源：site ${shortId(c.sources.skeleton_record)} · peak ${shortId(c.sources.peak_record)} · algo ${esc(c.sources.algo)} · 条件 ${esc(c.sources.conditions || "")}</div>
    </div>`;
  });
  panel.innerHTML = html;
  panel.querySelectorAll(".cand").forEach((el) =>
    el.addEventListener("click", () => {
      state.selectedCandidate = el.dataset.peak || null;
      renderDecide();
      panel.querySelectorAll(".cand").forEach((x) => x.style.outline = "");
      el.style.outline = "2px solid #5aa7ff";
    }));
  renderDecide();
}

function renderDecide() {
  const box = $("decide-panel");
  const res = currentResult();
  const readOnly = !!state.atEvent;
  if (!state.selectedSite || !res) { box.className = "empty"; box.textContent = "选择一个候选后可接受 / 否定。"; return; }
  const site = siteById(state.selectedSite);
  const cands = (res.candidate_map[site.id] || []);
  const chosen = cands.find((c) => (c.peak_id || "none") === (state.selectedCandidate || "none")) || cands[0];
  if (!chosen) { box.className = "empty"; box.textContent = "无可裁定候选。"; return; }
  box.className = "";
  const peakTxt = chosen.peak_id ? `峰 ${esc(peakById(chosen.peak_id)?.label)} ${peakById(chosen.peak_id)?.shift_ppm} ppm` : "unknown（缺峰占位）";
  box.innerHTML = `
    <div class="line" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <span>对象：<b>${esc(site.hydrogen !== "-" ? site.hydrogen : site.carbon)}</b> ↔ ${peakTxt}（分数 ${chosen.score.toFixed(3)}）</span>
      <input id="dec-actor" value="reviewer" style="width:110px"/>
      <input id="dec-rationale" placeholder="裁定理由（写入事件）" style="flex:1;min-width:180px"/>
      <button ${readOnly ? "disabled" : ""} id="btn-accept" class="primary">接受（新分支）</button>
      <button ${readOnly ? "disabled" : ""} id="btn-reject">否定（新分支）</button>
    </div>
    <div class="hint">接受/否定都会生成 decision_made 事件和重算事件；在并列候选上两位编辑者的冲突会进入冲突档案。</div>`;
  if (!readOnly) {
    $("btn-accept").onclick = () => makeDecision(chosen, "accept");
    $("btn-reject").onclick = () => makeDecision(chosen, "reject");
  }
}

async function makeDecision(cand, verdict) {
  const head = state.snap.head_event;
  const actor = $("dec-actor").value || "reviewer";
  const rationale = $("dec-rationale").value;
  try {
    const out = await api(`/api/projects/${state.projectId}/decisions`, {
      method: "POST",
      body: {site_id: state.selectedSite, peak_id: cand.peak_id, result_id: currentResult().id,
             verdict, actor, rationale, parent_event: head, branch: `review-${actor}`}
    });
    state.atEvent = null;
    $("propagation").innerHTML = `<div>新分支 <b>${esc(out.branch)}</b>；决定事件 ${shortId(out.decision.event.id)}；重算事件 ${shortId(out.recomputed_event?.id)}。可让另一位编辑者对同一位点裁定以制造冲突。</div>`;
    toast("已写入裁定并传播");
    await refresh();
  } catch (e) { toast("裁定失败：" + e.message); }
}

// ---------- 选择与刷新 ----------
function selectSite(siteId) {
  state.selectedSite = siteId;
  const res = currentResult();
  const cands = res?.candidate_map[siteId] || [];
  const firstPeakCand = cands.find((c) => c.peak_id) || cands[0];
  state.selectedCandidate = firstPeakCand ? firstPeakCand.peak_id : null;
  renderAll();
}
function selectPeak(peakId) {
  state.selectedPeak = peakId;
  const res = currentResult();
  if (res) {
    for (const [sid, cands] of Object.entries(res.candidate_map)) {
      if (cands.some((c) => c.peak_id === peakId)) { state.selectedSite = sid; break; }
    }
  }
  state.selectedCandidate = peakId;
  renderAll();
}

async function refresh() {
  if (!state.projectId) return;
  const url = `/api/projects/${state.projectId}/snapshot${state.atEvent ? `?at=${encodeURIComponent(state.atEvent)}` : ""}`;
  state.snap = await api(url);
  $("proj-name").textContent = state.projectId;
  const head = headEvent();
  $("head-badge").textContent = `HEAD ${head ? "#" + head.seq + " " + shortId(head.id) : ""}`;
  $("hist-banner").classList.toggle("hidden", !state.atEvent);
  $("btn-latest").classList.toggle("hidden", !state.atEvent);
  // 保持选择有效
  if (state.selectedSite && !siteById(state.selectedSite)) state.selectedSite = null;
  renderAll();
}

function renderAll() {
  renderSkeleton(); renderSpectrum(); renderPeaksTable(); renderEdgesTable();
  renderCandidates(); renderDocs(); renderFindings(); renderTimeline(); renderParamsBox();
}

// ---------- 原始输入 ----------
function renderDocs() {
  const host = $("docs-list");
  if (!host || !state.snap) return;
  let html = `<table><thead><tr><th>逻辑名</th><th>版本</th><th>格式</th><th>条件(溶剂/温度/参考/频率)</th><th>作者/说明</th><th>诊断</th><th>身份</th></tr></thead><tbody>`;
  state.snap.documents.forEach((d) => {
    const h = d.headers;
    const conds = `${esc(h.solvent || "?")} · ${esc(h["temperature-k"] || "?")}K · ${esc(h.reference || "?")} · ${esc(h["spectrometer-mhz"] || "?")}MHz`;
    const diags = d.diagnostics.length
      ? d.diagnostics.map((x) => `<div class="diag ${x.level}">${x.code} L${x.lineno}: ${esc(x.message)}</div>`).join("")
      : `<span class="pill current">无诊断</span>`;
    html += `<tr><td>${esc(d.logical_name)}</td><td>v${d.version}</td><td>${esc(d.format)}</td>
      <td><small>${conds}</small></td><td><small>${esc(d.author)}：${esc(d.change_note)}</small></td>
      <td>${diags}<details><summary class="hint">原文/记录</summary><pre class="iostatus">${esc(d.raw_text)}</pre></details></td>
      <td><span class="idbadge" title="${esc(d.doc_id)}">${shortId(d.doc_id)}</span></td></tr>`;
  });
  html += `</tbody></table>`;
  host.innerHTML = html;
}

function renderParamsBox() {
  const box = $("in-params");
  if (box && state.snap && !box.value) box.value = JSON.stringify(state.snap.params, null, 2);
}

// ---------- 反例档案 ----------
function renderFindings() {
  const host = $("findings-panel");
  if (!host || !state.snap) return;
  const findings = state.snap.scenario_findings || [];
  const order = ["OVERLAP_PEAK", "SYMMETRY_EQUIVALENT", "MISSING_EXPECTED_EDGE",
    "GLOBAL_SHIFT_BIAS", "TIED_BEST", "EDITOR_CONFLICT", "MISSING_PEAK",
    "UNMATCHED_PEAK", "UNCONVERTIBLE_INTENSITY", "UNCONVERTIBLE_SHIFT",
    "UNCONVERTIBLE_INTEGRAL", "MISSING_HEADER", "BAD_COLUMN_COUNT", "UNKNOWN_FORMAT"];
  const byCode = {};
  findings.forEach((f) => (byCode[f.code] ||= []).push(f));
  let html = `<table><thead><tr><th>判定码（机器事件）</th><th>数量</th><th>范围/说明</th><th>事件身份</th></tr></thead><tbody>`;
  Object.keys(byCode).sort((a, b) => order.indexOf(a) - order.indexOf(b)).forEach((code) => {
    const list = byCode[code];
    html += `<tr><td><b>${code}</b></td><td>${list.length}</td>
      <td>${list.slice(0, 3).map((f) => esc(f.scope)).join("<br>")}${list.length > 3 ? " …" : ""}</td>
      <td>${list.slice(0, 2).map((f) => `<span class="idbadge" title="${esc(f.id)}">${shortId(f.id)}</span>`).join(" ")}</td></tr>`;
  });
  html += `</tbody></table>`;
  host.innerHTML = html;

  const chost = $("conflicts-panel");
  let ch = `<table><thead><tr><th>冲突</th><th>位点</th><th>状态</th><th>决定（采纳/未采纳）</th></tr></thead><tbody>`;
  state.snap.conflicts.forEach((c) => {
    const ds = state.snap.decisions.filter((d) => d.conflict_id === c.id)
      .map((d) => `<div><span class="pill ${d.adopted ? "accept" : "reject"}">${d.adopted ? "采纳" : "未采纳"}</span> ${esc(d.actor)} ${d.verdict} peak ${shortId(d.peak_id)}</div>`).join("");
    ch += `<tr><td><span class="idbadge">${shortId(c.id)}</span></td><td>${shortId(c.site_id)}</td>
      <td><span class="pill ${c.status === "resolved" ? "current" : "warn"}">${c.status}</span></td><td>${ds}</td></tr>`;
  });
  ch += `</tbody></table>`;
  chost.innerHTML = ch;
}

// ---------- 时间线 ----------
function renderTimeline() {
  const host = $("timeline");
  if (!host || !state.snap) return;
  // 以 parent 链建树；分支缩进
  const events = state.snap.events;
  const byParent = {};
  events.forEach((e) => (byParent[e.parent_id || "ROOT"] ||= []).push(e));
  const depthOf = {};
  const calc = (e, d) => { depthOf[e.id] = d; (byParent[e.id] || []).forEach((ch2) => calc(ch2, d + 1)); };
  (byParent["ROOT"] || []).forEach((e) => calc(e, 0));
  let html = "";
  events.forEach((e) => {
    const undone = e.undone ? `<span class="pill reject">已撤销</span>` : "";
    const branch = e.branch !== "main" ? `<span class="pill info">${esc(e.branch)}</span>` : "";
    const desc = eventSummary(e);
    html += `<div class="tl-node ${e.branch !== "main" ? "branch" : ""}" style="margin-left:${10 + depthOf[e.id] * 18}px">
      <a data-event="${e.id}">#${e.seq} ${e.kind}</a> ${branch} ${undone}
      <span class="hint">${esc(e.actor)} · ${esc(e.at)}</span><div>${desc}</div></div>`;
  });
  host.innerHTML = html;
  host.querySelectorAll("[data-event]").forEach((el) =>
    el.addEventListener("click", () => checkout(el.dataset.event)));
}

function eventSummary(e) {
  const p = e.payload || {};
  switch (e.kind) {
    case "source_committed": return `源 ${esc(p.logical_name)} → v${p.version}（${esc(p.change_note)}）${p.has_error ? '<span class="diag error"> 含格式错误</span>' : ""}`;
    case "params_revised": return `参数 v${p.version}（指纹 ${esc(p.fingerprint)}）：${esc(p.note)}`;
    case "candidates_computed": return `重算 ${(p.result_ids || []).length} 个结果（${esc(p.reason)}）`;
    case "decision_made": return `${esc(p.verdict)} site ${shortId(p.site_id)} → peak ${shortId(p.peak_id)}：${esc(p.rationale)}`;
    case "conflict_resolved": return `解决冲突 ${shortId(p.conflict_id)}，采纳 ${shortId(p.winner_decision)}`;
    case "decision_undone": return `撤销决定事件 ${shortId(p.undone_event)}`;
    case "conditions_changed": return `条件变更 ${esc(p.change_kind)}：${esc(p.old_conditions_signature)} → ${esc(p.new_conditions_signature)}（仅使 ${esc(p.affected_nucleus)} 候选过期）`;
    default: return esc(JSON.stringify(p).slice(0, 120));
  }
}

async function checkout(eventId) {
  state.atEvent = eventId;
  await refresh();
  toast("已恢复该事件时点的依赖版本与可见结论（只读历史）");
}

// ---------- 按钮与 IO ----------
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}

async function loadDemo() {
  try {
    const out = await api("/api/demo-case", {method: "POST"});
    state.projectId = out.project_id;
    state.atEvent = null;
    await refresh();
    switchTab("workbench");
    toast("离线案例已载入：六类反例 + 两编辑者冲突 + 重放");
  } catch (e) { toast("载入失败：" + e.message); }
}

async function submitSource(asConditions) {
  const logical_name = $("in-name").value.trim();
  const text = $("in-text").value;
  const actor = $("in-actor").value || "reviewer";
  const change_note = $("in-note").value || "新版本";
  if (!logical_name || !text) { toast("需要逻辑名和文本"); return; }
  const parent = state.snap.head_event;
  try {
    const path = asConditions
      ? `/api/projects/${state.projectId}/conditions`
      : `/api/projects/${state.projectId}/sources`;
    const body = asConditions
      ? {logical_name, change_kind: "solvent-temperature-reference", text, actor, parent_event: parent}
      : {logical_name, text, actor, change_note, parent_event: parent};
    await api(path, {method: "POST", body});
    $("in-text").value = "";
    await refresh();
    toast(asConditions ? "条件变更已提交：仅相关核候选过期" : "新版本已提交（旧版本保留）");
  } catch (e) { toast("提交失败：" + e.message); }
}

async function submitParams() {
  let params;
  try { params = JSON.parse($("in-params").value); }
  catch (e) { toast("参数 JSON 不合法"); return; }
  const actor = $("in-actor").value || "spectroscopist";
  const note = $("in-note").value || "参数修订";
  await api(`/api/projects/${state.projectId}/params`,
    {method: "POST", body: {params, actor, note, parent_event: state.snap.head_event}});
  await refresh();
  toast("参数新版本已提交，旧结果标记过期但仍可查看");
}

async function doExport() {
  const bundle = await api(`/api/projects/${state.projectId}/export`);
  state.exportBundle = bundle;
  $("io-status").textContent = JSON.stringify({
    bundle_format: bundle.bundle_format, project_id: bundle.project_id,
    tables: Object.fromEntries(Object.entries(bundle.tables).map(([k, v]) => [k, v.length])),
    bundle_sha: bundle.bundle_sha
  }, null, 2);
  const blob = new Blob([JSON.stringify(bundle, null, 2)], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `nmrb-bundle-${state.projectId}.json`;
  a.click();
  toast("已导出含稳定身份的归档");
}

function doImportFile(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const bundle = JSON.parse(reader.result);
      const out = await api("/api/import-bundle", {method: "POST", body: {bundle}});
      state.projectId = out.project_id;
      state.atEvent = null;
      await refresh();
      toast("导入成功：身份、顺序与来源关系保持");
    } catch (e) { toast("导入失败：" + e.message); }
  };
  reader.readAsText(file);
}

async function populateCaseFiles() {
  const manifest = await api("/api/case-manifest");
  const names = [...manifest.baseline, ...manifest.mutations.map((m) => m.file).filter(Boolean),
                 ...manifest.bad_inputs];
  const sel = $("case-file-select");
  sel.innerHTML = names.map((n) => `<option>${esc(n)}</option>`).join("");
  sel.onchange = async () => {
    const f = await api(`/api/case-file?name=${encodeURIComponent(sel.value)}`);
    $("in-name").value = f.name.startsWith("exemplin-a-1h") || f.name.startsWith("exemplin-a-13c")
      ? f.name.replace(/\.(v[0-9][^.]*)\./, ".v1.")
      : f.name;
    $("in-text").value = f.text;
  };
  sel.onchange();
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-demo").onclick = loadDemo;
  $("btn-latest").onclick = async () => { state.atEvent = null; await refresh(); };
  document.querySelectorAll(".tab").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));
  $("nuc-toggle").onchange = (e) => { state.nucleus = e.target.value; renderSpectrum(); renderPeaksTable(); renderEdgesTable(); renderCandidates(); };
  $("btn-submit-source").onclick = () => submitSource(false);
  $("btn-conditions").onclick = () => submitSource(true);
  $("btn-submit-params").onclick = submitParams;
  $("btn-export").onclick = doExport;
  $("import-file").onchange = (e) => e.target.files[0] && doImportFile(e.target.files[0]);
  $("btn-import").onclick = () => $("import-file").click();
  populateCaseFiles();
});
