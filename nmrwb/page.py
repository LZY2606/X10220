"""复核界面单页（原生 JS + SVG）：谱峰、相关边、骨架、候选排名联动。"""
PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>NMR 共振经纬 · 归属候选工作台</title>
<style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f7f9;color:#222}
header{background:#1f3552;color:#fff;padding:10px 16px}
header h1{font-size:18px;margin:0 0 6px}
.toolbar button,.actions button{cursor:pointer}
main{padding:12px 16px;max-width:1180px;margin:auto}
section{background:#fff;border:1px solid #d9dde3;border-radius:8px;padding:10px 14px;margin:10px 0}
h2{font-size:15px;margin:4px 0 8px;color:#1f3552}
table{border-collapse:collapse;width:100%;font-size:12.5px}
td,th{border:1px solid #d9dde3;padding:2px 6px;text-align:left;vertical-align:top}
th{background:#eef2f7}
.idtag{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:#8a93a3}
.badge{display:inline-block;padding:0 5px;border-radius:8px;font-size:10.5px;margin-right:3px}
.b-best{background:#dff5dd}
.b-alt{background:#eee}
.b-stale{background:#f2e6e6;color:#9a5b5b}
.b-ov{background:#fff2cc}
.b-sym{background:#e4e9ff}
.b-miss{background:#ffe2d1}
.b-bias{background:#f8d7da}
.b-shared{background:#e7f6f2}
.stale-row{opacity:.55}
.hl{outline:2px solid #d9480f;background:#fff4e6 !important}
pre{white-space:pre-wrap;word-break:break-all;font-size:11.5px;background:#f1f3f5;padding:6px;border-radius:4px;max-height:220px;overflow:auto}
textarea{width:100%;height:120px;font-family:ui-monospace,monospace;font-size:11.5px}
input,select{font-size:12.5px}
button{font-size:12px;margin:1px}
#banner{background:#b58900;color:#111;padding:4px 8px;border-radius:4px;display:none;margin-top:4px;font-size:12.5px}
#msg{font-size:12px;color:#0b7a3b;margin-left:8px}
.muted{color:#777;font-size:11.5px}
.flex{display:flex;gap:14px;flex-wrap:wrap}
.flex>div{flex:1;min-width:320px}
.flagbox{font-size:10.5px}
</style>
</head>
<body>
<header>
<h1>NMR 共振经纬：化学位移、耦合候选与归属证据室</h1>
<div class="toolbar">
<button onclick="loadCasepack()">① 载入离线案例包</button>
<button onclick="doCompute()">② 计算候选</button>
<button onclick="doExport()">导出档案</button>
<button onclick="doReset()">删除本地数据库</button>
编辑者 <input id="editor" value="alice" size="8">
<span id="msg"></span>
</div>
<div id="banner"></div>
</header>
<main>
<section>
<h2>导入（原始输入保留，修订以新版本表达）</h2>
名称 <input id="in_name" value="h1" size="6">
格式 <select id="in_fmt"><option>nmrwb-1d</option><option>nmrwb-2d</option><option>nmrwb-mol</option></select>
<button onclick="submitInput()">提交输入</button>
<button onclick="submitParams()">提交参数修订</button>
<textarea id="in_raw" placeholder="# nmrwb-1d v1&#10;nucleus: 1H&#10;solvent: CDCl3&#10;frequency_mhz: 400.13&#10;temperature_k: 298.0&#10;reference: TMS&#10;peak 0.86 1000 6.0 d"></textarea>
<span class="muted">参数修订 JSON，例如 {"shift_tolerance_ppm": 0.40}，可直接点“提交参数修订”提交默认空修订 {}。</span>
</section>
<section><h2>输入版本（原文 / 父版本 / 诊断）</h2><div id="inputs"></div></section>
<section><h2>谱峰（化学位移 ppm · 峰强 · 积分 · 多重性）</h2><div id="peaks"></div></section>
<section><h2>相关边（COSY / HSQC / HMBC，只作证据）</h2><div id="edges"></div></section>
<section class="flex">
<div><h2>分子骨架 SVG</h2><div id="mol"></div></div>
<div><h2>耦合相关网络 SVG</h2><div id="net"></div></div>
</section>
<section><h2>归属候选排名（每个核保留全部合法候选）</h2><div id="cands"></div></section>
<section><h2>最近一次裁定的传播影响</h2><pre id="impact">（尚无人工决定）</pre></section>
<section class="flex">
<div><h2>分支与冲突</h2><div id="branches"></div><div id="conflicts"></div></div>
<div><h2>人工决定（冲突解决与撤销均入历史）</h2><div id="decisions"></div></div>
</section>
<section><h2>事件时间线（回到任意旧事件恢复当时结论）</h2><div id="timeline"></div></section>
<section>
<h2>导出 / 删除 / 再导入（身份、顺序、来源关系保持）</h2>
<textarea id="archive" placeholder="点击“导出档案”后内容出现在这里；可删除本地数据库再粘贴回来导入。"></textarea>
<button onclick="doImport()">导入档案</button>
</section>
</main>
<script>
let STATE=null, VIEW_SEQ=null;
async function api(path, opts){
  const r=await fetch(path,opts);
  if(!r.ok){const t=await r.text();throw new Error(t);}
  return r;
}
function msg(t,ok=true){const m=document.getElementById('msg');m.style.color=ok?'#0b7a3b':'#c92a2a';m.textContent=t;}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function idt(id){return '<span class="idtag">'+esc(id)+'</span>';}
function post(path,body){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());}
async function refresh(){
  const url='/api/state'+(VIEW_SEQ?('?seq='+VIEW_SEQ):'');
  STATE=await (await api(url)).json();
  renderAll();
}
function latestInputs(){return STATE.inputs;}
function latestInput(name){const id=STATE.inputs[name];return id?STATE.input_versions[id]:null;}
function renderAll(){
  renderBanner();renderInputs();renderPeaks();renderEdges();renderMol();renderNet();
  renderCands();renderBranches();renderConflicts();renderDecisions();renderTimeline();
}
function renderBanner(){
  const b=document.getElementById('banner');
  if(VIEW_SEQ){b.style.display='block';
    b.innerHTML='正在重放历史事件 #'+VIEW_SEQ+'：依赖版本与可见结论已恢复到当时，未被最新计算覆盖。 <button onclick="backToLatest()">返回最新状态</button>';}
  else{b.style.display='none';}
}
function backToLatest(){VIEW_SEQ=null;refresh();}
function renderInputs(){
  const vers=Object.values(STATE.input_versions).sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:a.version-b.version);
  let h='<table><tr><th>名称</th><th>版本</th><th>父版本</th><th>格式</th><th>身份</th><th>诊断</th></tr>';
  for(const v of vers){
    const parent=v.parent?idt(v.parent):'<span class="muted">—</span>';
    const dg=(v.diagnostics||[]).length;
    h+='<tr><td>'+esc(v.name)+'</td><td>v'+v.version+(v.superseded_by?' <span class="badge b-stale">已被取代</span>':'')+'</td><td>'+parent+'</td><td>'+esc(v.format)+'</td><td>'+idt(v.input_id)+'</td><td>'+(dg?('<span style="color:#c92a2a">'+dg+' 条</span>'):'0')+'</td></tr>';
    h+='<tr><td colspan="6"><details><summary class="muted">原始输入（不可覆盖）</summary><pre>'+esc(v.raw)+'</pre>';
    if(dg)h+='<pre style="color:#c92a2a">'+esc(JSON.stringify(v.diagnostics,null,1))+'</pre>';
    h+='</details></td></tr>';
  }
  document.getElementById('inputs').innerHTML=h+'</table>';
}
function renderPeaks(){
  let h='';
  for(const name of Object.keys(STATE.inputs)){
    const v=latestInput(name);
    if(!v||v.format!=='nmrwb-1d')continue;
    const m=v.parsed.meta;
    h+='<p><b>'+esc(name)+' ('+esc(m.nucleus)+')</b> · 溶剂 '+esc(m.solvent)+' · 频率 '+esc(m.frequency_mhz)+' MHz · 温度 '+esc(m.temperature_k)+' K · 参考 '+esc(m.reference)+'</p>';
    h+='<table><tr><th>身份</th><th>δ ppm（原始）</th><th>峰强</th><th>积分</th><th>多重性</th><th>诊断</th></tr>';
    for(const p of v.parsed.peaks){
      const bad=p.shift==null;
      h+='<tr data-hl="peak:'+p.peak_id+'"><td>'+idt(p.peak_id)+'</td>'
        +'<td>'+(bad?'<span class="badge b-miss">未转换</span> ':'')+esc(p.shift_raw)+'</td>'
        +'<td>'+esc(p.intensity_raw)+'</td><td>'+esc(p.integration_raw)+'</td><td>'+esc(p.multiplicity)+'</td>'
        +'<td>'+(bad?'<span class="muted">原值保留，附诊断</span>':'')+'</td></tr>';
    }
    h+='</table>';
  }
  document.getElementById('peaks').innerHTML=h;
}
function renderEdges(){
  let h='';
  for(const name of Object.keys(STATE.inputs)){
    const v=latestInput(name);
    if(!v||v.format!=='nmrwb-2d')continue;
    h+='<p><b>'+esc(name)+' ('+esc(v.parsed.meta.type)+')</b></p>';
    h+='<table><tr><th>身份</th><th>F1 ppm</th><th>F2 ppm</th><th>备注</th></tr>';
    for(const e of v.parsed.edges){
      const bad=e.f1==null||e.f2==null;
      h+='<tr data-hl="edge:'+e.edge_id+'"><td>'+idt(e.edge_id)+'</td><td>'+esc(e.f1_raw)+'</td><td>'+esc(e.f2_raw)+'</td><td>'+esc(e.note)+(bad?' <span class="badge b-miss">未转换</span>':'')+'</td></tr>';
    }
    h+='</table>';
  }
  document.getElementById('edges').innerHTML=h;
}
function atomPos(atoms,a){
  const at=atoms.find(x=>x.atom_id===a);
  if(at&&at.x!=null&&at.y!=null)return[at.x,at.y];
  return [40+70*(atoms.indexOf(at)%5),40+70*Math.floor(atoms.indexOf(at)/5)];
}
function renderMol(){
  const v=latestInput('mol');
  if(!v){document.getElementById('mol').innerHTML='<span class="muted">尚无分子骨架</span>';return;}
  const atoms=v.parsed.atoms,bonds=v.parsed.bonds;
  let xs=atoms.map(a=>a.x!=null?a.x:0),ys=atoms.map(a=>a.y!=null?a.y:0);
  let svg='<svg width="460" height="230" viewBox="0 0 380 210" style="background:#fafbfc;border:1px solid #e5e7eb">';
  for(const b of bonds){
    const p1=atomPos(atoms,b.a),p2=atomPos(atoms,b.b);
    svg+='<line x1="'+p1[0]+'" y1="'+p1[1]+'" x2="'+p2[0]+'" y2="'+p2[1]+'" stroke="#555" stroke-width="2"/>';
  }
  for(const a of atoms){
    const p=atomPos(a,a.atom_id);
    const col=a.element==='C'?'#cfd8e3':'#bcd0f0';
    svg+='<g data-hl="atom:'+a.atom_id+'"><circle cx="'+p[0]+'" cy="'+p[1]+'" r="13" fill="'+col+'" stroke="#446"/>'
      +'<text x="'+p[0]+'" y="'+(p[1]+3)+'" font-size="9" text-anchor="middle">'+esc(a.atom_id)+'</text></g>';
  }
  document.getElementById('mol').innerHTML=svg+'</svg><div class="muted">'+idt(v.input_id)+' · 坐标基准：画布 px</div>';
}
function laneOf(nuc){return nuc==='1H'?55:165;}
function renderNet(){
  const peaks={},edges=[];
  for(const name of Object.keys(STATE.inputs)){
    const v=latestInput(name);if(!v)continue;
    if(v.format==='nmrwb-1d'){const nuc=v.parsed.meta.nucleus;peaks[nuc]=v.parsed.peaks;}
    if(v.format==='nmrwb-2d')edges.push(v);
  }
  if(!Object.keys(peaks).length){document.getElementById('net').innerHTML='<span class="muted">尚无谱峰</span>';return;}
  const scale={};
  for(const nuc of Object.keys(peaks)){
    const sh=peaks[nuc].filter(p=>p.shift!=null).map(p=>p.shift);
    let lo=Math.min.apply(null,sh),hi=Math.max.apply(null,sh);
    if(hi-lo<0.2){lo-=0.1;hi+=0.1;}
    scale[nuc]=[lo,hi];
  }
  function X(nuc,v){const[lo,hi]=scale[nuc];return 40+(560-40)*(1-(v-lo)/(hi-lo));}
  let svg='<svg width="580" height="230" viewBox="0 0 600 220" style="background:#fafbfc;border:1px solid #e5e7eb">';
  for(const nuc of Object.keys(peaks)){
    const y=laneOf(nuc);
    svg+='<text x="14" y="'+(y+3)+'" font-size="11">'+nuc+'</text><line x1="40" y1="'+y+'" x2="560" y2="'+y+'" stroke="#ccc"/>';
    for(const p of peaks[nuc]){
      if(p.shift==null)continue;
      svg+='<g data-hl="peak:'+p.peak_id+'"><circle cx="'+X(nuc,p.shift)+'" cy="'+y+'" r="6" fill="#fff" stroke="#1f3552" stroke-width="1.5"/>'
        +'<text x="'+X(nuc,p.shift)+'" y="'+(y-9)+'" font-size="8.5" text-anchor="middle">'+esc(p.shift_raw)+'</text></g>';
    }
  }
  const col={COSY:'#2f6fb0',HSQC:'#2f9e44',HMBC:'#e8590c'};
  for(const v of edges){
    const t=v.parsed.meta.type.toUpperCase();
    for(const e of v.parsed.edges){
      if(e.f1==null||e.f2==null)continue;
      let n1,n2;
      if(t==='COSY'){n1=['1H',e.f1];n2=['1H',e.f2];}
      else{n1=['1H',e.f1];n2=['13C',e.f2];}
      if(!scale[n1[0]]||!scale[n2[0]])continue;
      const x1=X(n1[0],n1[1]),y1=laneOf(n1[0]),x2=X(n2[0],n2[1]),y2=laneOf(n2[0]);
      svg+='<line data-hl="edge:'+e.edge_id+'" x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+(col[t]||'#888')+'" stroke-width="1.5" opacity="0.75"/>';
    }
  }
  svg+='</svg><div class="muted">蓝=COSY 绿=HSQC 橙=HMBC；δ 轴高场在右（NMR 惯例），坐标基准 ppm</div>';
  document.getElementById('net').innerHTML=svg;
}
function flagBadges(c){
  const m={'overlap':['b-ov','重叠峰'],'symmetry':['b-sym','对称可互换'],'missing-edge':['b-miss','缺相关边'],
    'bias':['b-bias','整体偏置'],'shared-peak':['b-shared','共享峰']};
  return (c.flags||[]).map(f=>'<span class="badge '+(m[f]?m[f][0]:'b-alt')+'">'+(m[f]?m[f][1]:f)+'</span>').join('');
}
function renderCands(){
  const ids=Object.keys(STATE.candidate_sets);
  if(!ids.length){document.getElementById('cands').innerHTML='<span class="muted">尚未计算候选</span>';return;}
  let h='<div class="muted">候选集：'+ids.map(sid=>{const s=STATE.candidate_sets[sid];
    return idt(sid)+(s.stale?' <span class="badge b-stale">过期·仍可查看</span>':' <span class="badge b-best">当前</span>');}).join(' &nbsp; ')+'</div>';
  const s=STATE.candidate_sets[ids[ids.length-1]];
  const cond=s.conditions||{};
  h+='<p class="muted">算法 '+esc(s.algorithm)+' · 参数 '+idt(s.params_id)+' · 输入来源 '+idt(Object.values(s.input_versions).join('|')).slice(0,60)+
     '<br>条件：'+Object.keys(cond).map(n=>n+' 溶剂='+esc(cond[n].solvent)+' '+esc(cond[n].frequency_mhz)+'MHz '+esc(cond[n].temperature_k)+'K 参考='+esc(cond[n].reference)).join('；')+'</p>';
  if(s.diagnostics&&s.diagnostics.length){
    h+='<div class="flagbox">诊断：'+s.diagnostics.map(d=>'<span class="badge '+(d.type==='global_shift_bias'?'b-bias':'b-miss')+'">'+esc(d.type)+(d.nucleus?(':'+esc(d.nucleus)):'')+(d.value_ppm!=null?(' '+d.value_ppm+' ppm'):'')+'</span>').join(' ')+'</div>';
  }
  const byAtom={};
  for(const c of s.candidates){(byAtom[c.atom_id]=byAtom[c.atom_id]||[]).push(c);}
  h+='<table><tr><th>核</th><th>候选</th><th>峰身份</th><th>δ ppm</th><th>误差 ppm</th><th>分数</th><th>证据边</th><th>标记</th><th>裁定</th></tr>';
  for(const aid of Object.keys(byAtom).sort()){
    for(const c of byAtom[aid]){
      const ev=c.evidence.map(e=>'<span class="badge b-alt" data-hl="edge:'+e.edge_id+'">'+esc(e.type)+'</span>').join('');
      const decided=decisionStatus(c.candidate_id);
      h+='<tr class="'+(c.stale?'stale-row':'')+'"><td data-hl="atom:'+aid+'">'+esc(aid)+'</td>'
        +'<td><span class="badge '+(c.status==='best'?'b-best':'b-alt')+'">'+(c.status==='best'?'最优':'备选')+'</span>'+idt(c.candidate_id)+'</td>'
        +'<td data-hl="peak:'+c.peak_id+'">'+idt(c.peak_id)+'</td>'
        +'<td>'+esc(c.shift_ppm)+'</td><td>'+esc(c.err_ppm)+'</td><td>'+esc(c.score)+'</td>'
        +'<td class="flagbox">'+ev+'</td><td class="flagbox">'+flagBadges(c)+(c.stale?'<span class="badge b-stale">过期</span>':'')+(decided?'<span class="badge b-sym">'+decided+'</span>':'')+'</td>'
        +'<td class="actions">'+(VIEW_SEQ?'<span class="muted">历史视图</span>':
           '<button onclick="decide(\''+aid+'\',\''+c.candidate_id+'\',\'confirm\')">确认</button><button onclick="decide(\''+aid+'\',\''+c.candidate_id+'\',\'reject\')">否定</button>')+'</td></tr>';
    }
  }
  h+='</table>';
  if(s.unknowns&&s.unknowns.length){
    h+='<p class="muted">未知 / 无法唯一决定：'+s.unknowns.map(u=>esc(u.atom_id)+'（'+esc(u.reason)+'）').join('；')+'</p>';
  }
  document.getElementById('cands').innerHTML=h;
}
function decisionStatus(cid){
  const ds=Object.values(STATE.decisions).filter(d=>d.candidate_id===cid&&!d.undone);
  if(!ds.length)return '';
  return ds.map(d=>(d.action==='confirm'?'已确认 ':'已否定 ')+d.editor).join(',');
}
async function decide(atom,cid,action){
  try{
    const ev=await post('/api/decisions',{atom_id:atom,candidate_id:cid,action:action,editor:document.getElementById('editor').value||'anon'});
    document.getElementById('impact').textContent=JSON.stringify(ev.payload.impact,null,2);
    msg('裁定已写入新分支 '+ev.payload.new_branch);
    await refresh();
  }catch(e){msg(String(e),false);}
}
function renderBranches(){
  let h='<table><tr><th>分支</th><th>母分支</th><th>建立事件</th><th>编辑者</th></tr>';
  for(const name of Object.keys(STATE.branches).sort()){
    const b=STATE.branches[name];
    h+='<tr><td>'+esc(name)+'</td><td>'+esc(b.parent||'—')+'</td><td>#'+b.created_seq+'</td><td>'+esc(b.by||'')+'</td></tr>';
  }
  document.getElementById('branches').innerHTML=h+'</table>';
}
function renderConflicts(){
  const cs=Object.values(STATE.conflicts);
  if(!cs.length){document.getElementById('conflicts').innerHTML='<p class="muted">暂无冲突</p>';return;}
  let h='';
  for(const c of cs){
    h+='<div class="flagbox">冲突 '+idt(c.conflict_id)+' 核 '+esc(c.atom_id)+' 状态 '+(c.status==='open'?'<span class="badge b-bias">待解决</span>':'<span class="badge b-best">已解决</span>')+'<br>';
    for(const did of c.decisions){
      const d=STATE.decisions[did];
      h+='&nbsp;&nbsp;'+idt(did)+' '+esc(d.editor)+' → '+idt(d.candidate_id)+(d.undone?'（已撤销）':'');
      if(c.status==='open'&&!VIEW_SEQ)h+=' <button onclick="resolveIt(\''+c.conflict_id+'\',\''+did+'\')">采纳</button>';
      h+='<br>';
    }
    h+='</div>';
  }
  document.getElementById('conflicts').innerHTML=h;
}
async function resolveIt(cid,did){
  try{
    await post('/api/conflicts/'+cid+'/resolve',{winner_decision_id:did,editor:document.getElementById('editor').value||'anon'});
    msg('冲突解决已写入历史');await refresh();
  }catch(e){msg(String(e),false);}
}
function renderDecisions(){
  const ds=Object.values(STATE.decisions).sort((a,b)=>a.decision_id<b.decision_id?-1:1);
  let h='<table><tr><th>身份</th><th>编辑者</th><th>核</th><th>候选</th><th>动作</th><th>分支</th><th>操作</th></tr>';
  for(const d of ds){
    h+='<tr class="'+(d.undone?'stale-row':'')+'"><td>'+idt(d.decision_id)+'</td><td>'+esc(d.editor)+'</td><td data-hl="atom:'+d.atom_id+'">'+esc(d.atom_id)+'</td>'
      +'<td>'+idt(d.candidate_id)+'</td><td>'+(d.action==='confirm'?'确认':'否定')+'</td>'
      +'<td>'+esc(d.branch)+' → '+esc(d.new_branch)+(d.undone?' <span class="badge b-stale">已撤销</span>':'')+'</td>'
      +'<td>'+(VIEW_SEQ||d.undone?'':'<button onclick="undoIt(\''+d.decision_id+'\')">撤销</button>')+'</td></tr>';
  }
  document.getElementById('decisions').innerHTML=(ds.length?h:'<span class="muted">尚无人工决定</span>')+(ds.length?'</table>':'');
}
async function undoIt(did){
  try{await post('/api/undo',{decision_id:did,editor:document.getElementById('editor').value||'anon'});msg('撤销已写入历史');await refresh();}
  catch(e){msg(String(e),false);}
}
function renderTimeline(){
  let h='<table><tr><th>#</th><th>事件</th><th>身份</th><th>分支</th><th>时间</th><th>重放</th></tr>';
  for(const e of STATE.timeline){
    h+='<tr><td>'+e.seq+'</td><td>'+esc(e.kind)+'</td><td>'+idt(e.event_id)+'</td><td>'+esc(e.branch)+'</td><td>'+esc(e.clock)+'</td>'
      +'<td><button onclick="replay('+e.seq+')">回到此事件</button></td></tr>';
  }
  document.getElementById('timeline').innerHTML=h+'</table>';
}
function replay(seq){VIEW_SEQ=seq;refresh();}
async function submitInput(){
  try{
    const ev=await post('/api/inputs',{name:document.getElementById('in_name').value,format:document.getElementById('in_fmt').value,raw:document.getElementById('in_raw').value});
    msg('输入版本已提交：'+ev.payload.input_id);await refresh();
  }catch(e){msg(String(e),false);}
}
async function submitParams(){
  let upd={},txt=document.getElementById('in_raw').value;
  try{upd=txt.trim()?JSON.parse(txt):{};}catch(e){msg('参数修订必须是 JSON 对象',false);return;}
  try{await post('/api/params',{updates:upd});msg('参数版本已修订（旧候选按依赖范围过期）');await refresh();}
  catch(e){msg(String(e),false);}
}
async function doCompute(){try{const ev=await post('/api/compute',{});msg('候选集已生成：'+ev.payload.set_id);await refresh();}catch(e){msg(String(e),false);}}
async function loadCasepack(){try{const r=await post('/api/casepack/load',{});msg('已载入案例包：'+r.title+'（'+r.steps+' 步）');await refresh();}catch(e){msg(String(e),false);}}
async function doExport(){document.getElementById('archive').value=await (await api('/api/export')).text();msg('档案已导出');}
async function doImport(){
  try{const r=await post('/api/import',{archive:document.getElementById('archive').value});msg('已导入 '+r.imported+' 个事件，身份与顺序保持');VIEW_SEQ=null;await refresh();}
  catch(e){msg(String(e),false);}
}
async function doReset(){if(!confirm('删除本地数据库（事件存储）？'))return;try{await post('/api/reset',{});VIEW_SEQ=null;msg('本地数据库已清空');await refresh();}catch(e){msg(String(e),false);}}
document.addEventListener('mouseover',function(e){
  const t=e.target.closest('[data-hl]');if(!t)return;
  document.querySelectorAll('.hl').forEach(x=>x.classList.remove('hl'));
  document.querySelectorAll('[data-hl="'+t.getAttribute('data-hl')+'"]').forEach(x=>x.classList.add('hl'));
});
refresh();
</script>
</html>
"""
