const $ = (id) => document.getElementById(id);
let last = null;
let scenarios = [];
let paperData = null;
const savedToken = localStorage.getItem('l1_scheduler_token') || '';
if (savedToken) $('tokenInput').value = savedToken;

function headers(json = false) {
  const token = $('tokenInput').value.trim();
  if (token) localStorage.setItem('l1_scheduler_token', token);
  return Object.assign(json ? {'Content-Type':'application/json'} : {}, token ? {'Authorization': `Bearer ${token}`} : {});
}
async function api(path, options = {}) {
  const response = await fetch(path, Object.assign({}, options, {headers: Object.assign(headers(Boolean(options.body)), options.headers || {})}));
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }
function time(value) { if (!value) return ''; const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleTimeString('zh-CN',{hour12:false}); }
function setConnection(ok, text) { $('connection').classList.toggle('ok', ok); $('connection').lastChild.textContent = text; }

function renderStats(data) {
  const s = data.stats || {};
  $('statTotal').textContent = s.total || 0;
  $('statSucceeded').textContent = s.succeeded || 0;
  $('statRunning').textContent = s.RUNNING || 0;
  $('statQueue').textContent = `${s.QUEUED || 0} / ${(s['FAILED-LINK'] || 0) + (s.UNSCHEDULED || 0) + (s['FAILED-TIMEOUT'] || 0) + (s.FAILED || 0)}`;
  $('statRate').textContent = s.total ? `${((s.succeeded || 0) / s.total * 100).toFixed(1)}%` : '—';
  $('lastUpdate').textContent = `更新于 ${time(data.server_time)}`;
  $('modeBadge').textContent = `${String(data.fabric || data.mode || 'HYBRID').toUpperCase()}`;
  if (data.reality && data.reality.note) $('realityNote').textContent = data.reality.note;
  const w = (data.algorithm && data.algorithm.weights) || {};
  if (w.wl != null) $('statWeights').textContent = `wl${w.wl}`;
}

function renderDecision(decision) {
  const box = $('decisionBody');
  if (!decision) {
    box.innerHTML = '<div class="empty">提交任务后，这里展示 Score = wl·S(t)·Nlat + wc·Ncost + we·Nenergy + wld·Load</div>';
    $('decisionMeta').textContent = '等待调度';
    return;
  }
  $('decisionMeta').textContent = decision.selected_region
    ? `选中 ${decision.selected_region} · ${Number(decision.compute_ms || 0).toFixed(3)} ms · S(t)=${Number(decision.s_t || 1).toFixed(3)}`
    : (decision.reason || '未调度');
  const metrics = decision.metrics || [];
  if (!metrics.length) {
    box.innerHTML = `<div class="empty">${esc(decision.reason || '无可行候选')}<br><small>${esc(JSON.stringify(decision.rejected || []))}</small></div>`;
    return;
  }
  const maxScore = Math.max(...metrics.map(m => Number(m.score) || 0), 1e-6);
  box.innerHTML = `<div class="score-list">${metrics.map(m => {
    const best = m.node === decision.selected_region;
    const width = Math.max(6, (1 - (Number(m.score) / (maxScore * 1.15))) * 100);
    return `<div class="score-item ${best ? 'best' : ''}">
      <strong>${esc(m.node)}</strong>
      <div class="bar"><i style="width:${width}%"></i></div>
      <span class="hash">${Number(m.score).toFixed(4)}</span>
      <span class="task-child" style="grid-column:1/-1">lat ${Number(m.latency_ms).toFixed(1)}ms · cost ${Number(m.cost).toFixed(2)} · energy ${Number(m.energy).toFixed(3)} · load ${Number(m.load).toFixed(2)}</span>
    </div>`;
  }).join('')}</div>
  <p class="subtle" style="margin-top:8px">${esc(decision.reason || '')}</p>`;
}

function renderNodes(nodes) {
  $('nodes').innerHTML = (nodes || []).map(node => {
    const sim = !!node.simulated;
    const online = node.healthy && node.link_up && node.reachable !== false;
    const reach = sim ? '● SIM' : (node.reachable === false ? 'AGENT DOWN' : (online ? '● ONLINE' : '● OFFLINE'));
    const cls = sim ? 'sim' : (online ? 'online' : 'offline');
    const tee = node.has_tee ? ' · TEE' : '';
    const green = node.green_factor != null ? ` · 绿电 ${(Number(node.green_factor)*100).toFixed(0)}%` : '';
    return `<div class="node-card">
      <div class="node-top"><div class="region">${esc(node.region)}</div><span class="node-status ${cls}">${reach}</span></div>
      <div class="node-meta">${esc(node.model || '')} · RTT ${node.rtt_ms ?? '—'} ms · ¥${Number(node.cost||0).toFixed(2)}/卡时 · 可用 ${Number(node.free_gb||0).toFixed(1)} GB${tee}${green}${node.agent_url? ' · '+esc(node.agent_url):''}${node.last_error? ' · '+esc(node.last_error):''}</div>
      ${(node.gpus||[]).slice(0,6).map(gpu => `<div class="gpu"><div class="gpu-line"><span>${esc(gpu.id)}</span><span>${Number(gpu.free_gb||0).toFixed(1)} / ${gpu.total_gb||'?'} GB · util ${gpu.utilization_pct||0}%</span></div><div class="meter"><i style="width:${Math.min(100,gpu.load_pct||gpu.utilization_pct||0)}%"></i></div></div>`).join('')}
      ${(node.gpus||[]).length > 6 ? `<div class="task-child">…另有 ${(node.gpus.length-6)} 张卡</div>` : ''}
      <div class="node-actions"><button class="button ghost" onclick="toggleNode('${esc(node.region)}','healthy')">${node.healthy?'节点离线':'恢复节点'}</button><button class="button ghost" onclick="toggleNode('${esc(node.region)}','link_up')">${node.link_up?'断开链路':'恢复链路'}</button></div>
    </div>`;
  }).join('');
}

function statusTag(status) { return `<span class="status ${esc(status)}">${esc(status)}</span>`; }
function renderTasks(tasks) {
  const rows = (tasks || []).filter(x => x.type === 'child' || x.type === 'parent').slice(0, 80);
  if (!rows.length) { $('taskRows').innerHTML = '<tr><td colspan="6" class="empty">还没有任务</td></tr>'; return; }
  $('taskRows').innerHTML = rows.map(task => {
    const isParent = task.type === 'parent';
    const region = task.selected_region || (isParent ? (task.regions || []).join(' / ') : '—');
    const result = task.result_sha256 ? `<span class="hash">${esc(task.result_sha256.slice(0,18))}…</span>` : `<span class="reason">${esc(task.message || task.reason || '—')}</span>`;
    const code = task.scenario_code ? `<div class="task-child">${esc(task.scenario_code)} ${esc(task.scenario_title || '')}</div>` : '';
    return `<tr><td><div class="task-id">${esc(task.task_id)}</div>${isParent?code:'<div class="task-child">父任务 '+esc(task.parent_id)+'</div>'}</td><td>${isParent?'PARENT':'SHARD S'+String(task.shard).padStart(2,'0')}</td><td>${statusTag(task.status)}</td><td>${esc(region)}<br><span class="task-child">${esc(task.gpu_id || '—')}</span></td><td><div class="progress"><div class="meter"><i style="width:${task.progress||0}%"></i></div><em>${Math.round(task.progress||0)}%</em></div></td><td>${result}</td></tr>`;
  }).join('');
}

function renderEvents(events) {
  $('events').innerHTML = (events || []).length ? events.slice(0,25).map(e => `<div class="event"><time>${time(e.ts)}</time><div><b>${esc(e.event)} <span class="task-child">${esc(e.task_id)}</span></b><small>${esc(e.message || '')}</small></div></div>`).join('') : '<div class="empty">暂无事件</div>';
}

function fillScenarios(list) {
  scenarios = list || [];
  const select = $('scenario');
  select.innerHTML = scenarios.map(s => `<option value="${esc(s.id)}">[${esc(s.code)}] ${esc(s.title)}</option>`).join('');
  if (!scenarios.length) return;
  applyScenario(scenarios[0].id);
}

function applyScenario(id) {
  const s = scenarios.find(x => x.id === id) || scenarios[0];
  if (!s) return;
  $('scenarioCode').textContent = s.code;
  $('scenarioTitle').textContent = s.title;
  $('scenarioSummary').textContent = s.summary || '';
  $('scenarioDetail').textContent = s.detail || '';
  if (s.defaults) {
    if (s.defaults.shards != null) $('shards').value = s.defaults.shards;
    if (s.defaults.memory_gb != null) $('memory').value = s.defaults.memory_gb;
    if (s.defaults.mode) {
      const mode = s.defaults.mode === '加权平均' ? '动态权重多目标' : s.defaults.mode;
      $('mode').value = mode;
    }
  }
}

function drawLineChart(canvas, series, opts = {}) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 640;
  const cssH = canvas.clientHeight || 220;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = cssW, H = cssH;
  const pad = {l: 42, r: 14, t: 16, b: 28};
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, W, H);
  const allY = series.flatMap(s => s.data);
  if (!allY.length) return;
  let yMin = opts.min != null ? opts.min : Math.min(...allY);
  let yMax = opts.max != null ? opts.max : Math.max(...allY);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const n = Math.max(...series.map(s => s.data.length), 1);
  const xAt = (i) => pad.l + (W - pad.l - pad.r) * (n <= 1 ? 0 : i / (n - 1));
  const yAt = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v - yMin) / (yMax - yMin));
  ctx.strokeStyle = '#d7e0e6'; ctx.lineWidth = 1;
  for (let g = 0; g < 4; g++) {
    const y = pad.t + (H - pad.t - pad.b) * g / 3;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }
  ctx.fillStyle = '#5c6b7a'; ctx.font = '11px JetBrains Mono, monospace';
  ctx.fillText(yMax.toFixed(0), 4, pad.t + 4);
  ctx.fillText(yMin.toFixed(0), 4, H - pad.b);
  series.forEach(s => {
    if (!s.data.length) return;
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.beginPath();
    s.data.forEach((v, i) => { const x = xAt(i), y = yAt(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  });
}

function drawRadar(canvas, summaries) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 420;
  const cssH = canvas.clientHeight || 320;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  const names = Object.keys(summaries || {});
  if (!names.length) return;
  const axes = [
    { key: 'success_rate_pct', label: '成功率', max: 100 },
    { key: 'gpu_util_pct', label: 'GPU利用率', max: 100 },
    { key: 'lat_sat', label: '时延满意度', max: 100 },
    { key: 'cost_eco', label: '成本经济度', max: 100 },
    { key: 'avg_green_pct', label: '绿电比例', max: 100 },
  ];
  const cx = cssW / 2, cy = cssH / 2 + 8, R = Math.min(cssW, cssH) * 0.36;
  const colors = ['#0f766e','#b45309','#1d4e89','#7c2d12','#64748b','#047857'];
  axes.forEach((ax, i) => {
    const ang = -Math.PI / 2 + i * 2 * Math.PI / axes.length;
    for (let ring = 1; ring <= 4; ring++) {
      ctx.beginPath();
      axes.forEach((_, j) => {
        const a = -Math.PI / 2 + j * 2 * Math.PI / axes.length;
        const r = R * ring / 4;
        const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
        j ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.closePath(); ctx.strokeStyle = '#d7e0e6'; ctx.stroke();
    }
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + R * Math.cos(ang), cy + R * Math.sin(ang)); ctx.strokeStyle = '#c9d2da'; ctx.stroke();
    ctx.fillStyle = '#5c6b7a'; ctx.font = '11px Noto Sans SC, sans-serif';
    ctx.fillText(ax.label, cx + (R + 14) * Math.cos(ang) - 20, cy + (R + 14) * Math.sin(ang));
  });
  names.forEach((name, ni) => {
    const s = summaries[name];
    const latSat = Math.max(0, Math.min(100, 100 - (Number(s.avg_latency_ms || 0) - 40)));
    const costEco = Math.max(0, Math.min(100, 100 - Number(s.avg_cost || 0) * 8));
    const vals = [
      Number(s.success_rate_pct || 0),
      Number(s.gpu_util_pct || 0),
      latSat,
      costEco,
      Number(s.avg_green_pct || 0),
    ];
    ctx.beginPath();
    vals.forEach((v, i) => {
      const ang = -Math.PI / 2 + i * 2 * Math.PI / vals.length;
      const r = R * (v / 100);
      const x = cx + r * Math.cos(ang), y = cy + r * Math.sin(ang);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.closePath();
    ctx.strokeStyle = colors[ni % colors.length];
    ctx.lineWidth = name.includes('本文') ? 3 : 1.5;
    ctx.stroke();
  });
}

function renderPaper(data) {
  paperData = data;
  const summaries = data.summaries || {};
  const rows = Object.entries(summaries);
  if (!rows.length) return;
  $('paperRows').innerHTML = rows.map(([name, s]) => `<tr>
    <td>${esc(name)}</td>
    <td><strong>${Number(s.success_rate_pct).toFixed(2)}%</strong></td>
    <td>${Number(s.avg_latency_ms).toFixed(2)}</td>
    <td>${Number(s.avg_cost).toFixed(2)}</td>
    <td>${Number(s.avg_energy).toFixed(2)}</td>
    <td>${Number(s.avg_compute_ms).toFixed(4)}</td>
    <td class="task-child">${esc((s.failed_ids || []).join(', ') || '—')}</td>
  </tr>`).join('');
  drawRadar($('chartRadar'), summaries);
  const paper = summaries['本文方法（动态权重多目标调度）'] || {};
  const ga = summaries['遗传算法'] || {};
  const speed = (ga.avg_compute_ms && paper.avg_compute_ms) ? (ga.avg_compute_ms / paper.avg_compute_ms) : null;
  $('paperNote').textContent = `动态权重多目标成功率 ${paper.success_rate_pct}%；失败 ${ (paper.failed_ids||[]).join('/') || '无' }。`
    + (speed ? ` 相对遗传算法加速约 ${speed.toFixed(0)}×。` : '')
    + ' 消融细节见服务端 evidence。';
}

function renderCharts(metrics) {
  const pts = metrics || [];
  drawLineChart($('chartFree'), [
    { color: '#0f766e', data: pts.map(p => Number(p.hainan_free_gb || 0)) },
    { color: '#b45309', data: pts.map(p => Number(p.chongqing_free_gb || 0)) },
  ]);
  drawLineChart($('chartUtil'), [
    { color: '#0f766e', data: pts.map(p => Number(p.hainan_util_pct || 0)) },
    { color: '#b45309', data: pts.map(p => Number(p.chongqing_util_pct || 0)) },
  ], { min: 0, max: 100 });
  drawLineChart($('chartTasks'), [
    { color: '#1d4e89', data: pts.map(p => Number(p.running || 0)) },
    { color: '#b45309', data: pts.map(p => Number(p.queued || 0)) },
    { color: '#047857', data: pts.map(p => Number(p.succeeded_shards || 0)) },
  ], { min: 0 });
}

async function refresh() {
  try {
    const data = await api('/api/status');
    last = data;
    renderStats(data);
    renderNodes(data.nodes || []);
    renderTasks(data.tasks || []);
    renderEvents(data.events || []);
    renderCharts(data.metrics || []);
    renderDecision(data.last_decision);
    if (data.paper_summary && data.paper_summary.summaries) {
      renderPaper({ summaries: data.paper_summary.summaries, ablation: data.paper_summary.ablation });
    }
    if (!scenarios.length && data.scenarios) fillScenarios(data.scenarios);
    setConnection(true, '已连接');
  } catch (err) {
    setConnection(false, String(err.message || err));
  }
}

$('scenario').addEventListener('change', (e) => applyScenario(e.target.value));
$('taskForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        scenario: $('scenario').value,
        shards: Number($('shards').value),
        memory_gb: Number($('memory').value),
        mode: $('mode').value,
        task_id: $('taskId').value.trim() || undefined,
      }),
    });
    $('taskId').value = '';
    await refresh();
  } catch (err) {
    alert(`提交失败：${err.message || err}`);
  }
});
$('resetBtn').addEventListener('click', async () => {
  await api('/api/reset', { method: 'POST', body: '{}' });
  await refresh();
});
$('paperBtn').addEventListener('click', async () => {
  $('paperBtn').disabled = true;
  $('paperBtn').textContent = '实验中…';
  try {
    const data = await api('/api/paper/experiment', { method: 'POST', body: '{}' });
    renderPaper(data);
  } catch (err) {
    alert(`对比实验失败：${err.message || err}`);
  } finally {
    $('paperBtn').disabled = false;
    $('paperBtn').textContent = '跑对比实验';
  }
});
window.toggleNode = async (region, field) => {
  await api('/api/nodes/toggle', { method: 'POST', body: JSON.stringify({ region, field }) });
  await refresh();
};

refresh();
setInterval(refresh, 1500);
