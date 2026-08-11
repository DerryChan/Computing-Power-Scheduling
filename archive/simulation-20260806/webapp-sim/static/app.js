const $ = (id) => document.getElementById(id);
let last = null;
let scenarios = [];
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
  $('modeBadge').textContent = String(data.mode || 'SIMULATION').toUpperCase();
  if (data.reality && data.reality.note) $('realityNote').textContent = data.reality.note;
}

function renderNodes(nodes) {
  $('nodes').innerHTML = nodes.map(node => {
    const online = node.healthy && node.link_up;
    return `<div class="node-card">
      <div class="node-top"><div class="region">${esc(node.region)}</div><span class="node-status ${online?'online':'offline'}">${online?'● ONLINE':'● OFFLINE'}</span></div>
      <div class="node-meta">${esc(node.model)} · RTT ${node.rtt_ms} ms · 成本 ${node.cost.toFixed(2)} · 可用 ${node.free_gb.toFixed(1)} GB</div>
      ${node.gpus.map(gpu => `<div class="gpu"><div class="gpu-line"><span>${esc(gpu.id)}</span><span>${gpu.free_gb.toFixed(1)} / ${gpu.total_gb} GB free · util ${gpu.utilization_pct||0}%</span></div><div class="meter"><i style="width:${Math.min(100,gpu.load_pct)}%"></i></div></div>`).join('')}
      <div class="node-actions"><button class="button ghost" onclick="toggleNode('${esc(node.region)}','healthy')">${node.healthy?'节点离线':'恢复节点'}</button><button class="button ghost" onclick="toggleNode('${esc(node.region)}','link_up')">${node.link_up?'断开链路':'恢复链路'}</button></div>
    </div>`;
  }).join('');
}

function statusTag(status) { return `<span class="status ${esc(status)}">${esc(status)}</span>`; }
function renderTasks(tasks) {
  const rows = tasks.filter(x => x.type === 'child' || x.type === 'parent').slice(0, 80);
  if (!rows.length) { $('taskRows').innerHTML = '<tr><td colspan="6" class="empty">还没有任务，提交一个实时测试开始</td></tr>'; return; }
  $('taskRows').innerHTML = rows.map(task => {
    const isParent = task.type === 'parent';
    const region = task.selected_region || (isParent ? (task.regions || []).join(' / ') : '—');
    const result = task.result_sha256 ? `<span class="hash">${esc(task.result_sha256.slice(0,18))}…</span>` : `<span class="reason">${esc(task.message || task.reason || '—')}</span>`;
    const code = task.scenario_code ? `<div class="task-child">${esc(task.scenario_code)} ${esc(task.scenario_title || '')}</div>` : '';
    return `<tr><td><div class="task-id">${esc(task.task_id)}</div>${isParent?code:'<div class="task-child">父任务 '+esc(task.parent_id)+'</div>'}</td><td>${isParent?'PARENT':'SHARD S'+String(task.shard).padStart(2,'0')}</td><td>${statusTag(task.status)}</td><td>${esc(region)}<br><span class="task-child">${esc(task.gpu_id || '—')}</span></td><td><div class="progress"><div class="meter"><i style="width:${task.progress||0}%"></i></div><em>${Math.round(task.progress||0)}%</em></div></td><td>${result}</td></tr>`;
  }).join('');
}

function renderEvents(events) {
  $('events').innerHTML = events.length ? events.slice(0,35).map(e => `<div class="event"><time>${time(e.ts)}</time><div><b>${esc(e.event)} <span class="task-child">${esc(e.task_id)}</span></b><small>${esc(e.message || '')}</small></div></div>`).join('') : '<div class="empty">暂无事件</div>';
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
    if (s.defaults.mode) $('mode').value = s.defaults.mode;
  }
}

/* ---- lightweight multi-series line charts (no CDN) ---- */
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
  ctx.fillStyle = 'rgba(7,18,32,0.55)';
  ctx.fillRect(0, 0, W, H);

  const allY = series.flatMap(s => s.data);
  let minY = opts.min != null ? opts.min : Math.min(0, ...allY);
  let maxY = opts.max != null ? opts.max : Math.max(1, ...allY);
  if (minY === maxY) { maxY = minY + 1; }
  const n = Math.max(...series.map(s => s.data.length), 1);
  const xAt = (i) => pad.l + (i / Math.max(1, n - 1)) * (W - pad.l - pad.r);
  const yAt = (v) => pad.t + (1 - (v - minY) / (maxY - minY)) * (H - pad.t - pad.b);

  ctx.strokeStyle = 'rgba(40,70,100,0.55)';
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const y = pad.t + (g / 4) * (H - pad.t - pad.b);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    const val = maxY - (g / 4) * (maxY - minY);
    ctx.fillStyle = '#7f98b2';
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillText(val.toFixed(opts.digits ?? 0), 6, y + 3);
  }

  series.forEach(s => {
    if (!s.data.length) return;
    ctx.beginPath();
    s.data.forEach((v, i) => {
      const x = xAt(i), y = yAt(v);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.stroke();
    const last = s.data[s.data.length - 1];
    ctx.fillStyle = s.color;
    ctx.beginPath();
    ctx.arc(xAt(s.data.length - 1), yAt(last), 3, 0, Math.PI * 2);
    ctx.fill();
  });
}

function renderCharts(metrics) {
  const pts = metrics || [];
  drawLineChart($('chartFree'), [
    {color: '#55a6ff', data: pts.map(p => p.hainan_free_gb)},
    {color: '#ffb454', data: pts.map(p => p.chongqing_free_gb)},
  ], {min: 0, max: 50, digits: 0});
  drawLineChart($('chartUtil'), [
    {color: '#55a6ff', data: pts.map(p => p.hainan_util_pct)},
    {color: '#ffb454', data: pts.map(p => p.chongqing_util_pct)},
  ], {min: 0, max: 100, digits: 0});
  drawLineChart($('chartTasks'), [
    {color: '#55a6ff', data: pts.map(p => p.running)},
    {color: '#ffb454', data: pts.map(p => p.queued)},
    {color: '#36d399', data: pts.map(p => p.succeeded_shards)},
  ], {min: 0, digits: 0});
}

async function refresh() {
  try {
    const data = await api('/api/status');
    last = data;
    if ((!scenarios.length) && data.scenarios) fillScenarios(data.scenarios);
    renderStats(data);
    renderNodes(data.nodes || []);
    renderTasks(data.tasks || []);
    renderEvents(data.events || []);
    renderCharts(data.metrics || []);
    setConnection(true, '已连接');
  } catch (error) {
    setConnection(false, '连接失败');
    console.warn(error);
  }
}

async function toggleNode(region, field) {
  try { await api('/api/nodes/toggle', {method:'POST', body: JSON.stringify({region, field})}); await refresh(); }
  catch (e) { alert(e.message); }
}
window.toggleNode = toggleNode;

$('scenario').addEventListener('change', () => applyScenario($('scenario').value));
$('taskForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        task_id: $('taskId').value.trim() || undefined,
        scenario: $('scenario').value,
        shards: Number($('shards').value),
        memory_gb: Number($('memory').value),
        mode: $('mode').value,
      }),
    });
    $('taskId').value = '';
    await refresh();
  } catch (e) { alert(e.message); }
  finally { button.disabled = false; }
});
$('resetBtn').addEventListener('click', async () => {
  if (confirm('确定清空当前仿真任务、事件和折线历史吗？')) {
    await api('/api/reset', {method:'POST', body:'{}'});
    await refresh();
  }
});
window.addEventListener('resize', () => { if (last) renderCharts(last.metrics || []); });
refresh();
setInterval(refresh, 1000);
