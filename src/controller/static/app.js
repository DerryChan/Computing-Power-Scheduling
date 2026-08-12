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
  $('nodes').innerHTML = (nodes || []).map(node => {
    const online = node.healthy && node.link_up && node.reachable !== false;
    const reach = node.reachable === false ? 'AGENT DOWN' : (online ? '● ONLINE' : '● OFFLINE');
    const gpus = node.gpus || [];
    const gpuHtml = gpus.map(gpu => {
      const free = Number(gpu.free_gb ?? 0);
      const total = Number(gpu.total_gb ?? 0);
      const used = Math.max(0, total - free);
      const memPct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
      const util = Math.min(100, Math.max(0, Number(gpu.utilization_pct ?? 0)));
      const peak = Math.min(100, Math.max(util, Number(gpu.peak_utilization_pct ?? 0)));
      const utilText = peak > 0.5
        ? `实时 ${Math.round(util)}% · 峰 ${Math.round(peak)}%`
        : `实时 ${Math.round(util)}%`;
      return `<div class="gpu">
        <div class="gpu-line"><span>${esc(gpu.id)}</span><span>${free.toFixed(1)} / ${total.toFixed(2)} GB free · ${utilText}</span></div>
        <div class="gpu-meters">
          <div class="meter-row"><span class="meter-label">利用率</span>
            <div class="meter util-meter" title="实心=实时利用率，浅底/竖线=本轮峰值">
              <span class="peak-fill" style="width:${peak}%"></span>
              <span class="live-fill" style="width:${util}%"></span>
              ${peak > 0.5 ? `<span class="peak-mark" style="left:${peak}%"></span>` : ''}
            </div>
          </div>
          <div class="meter-row"><span class="meter-label">显存</span>
            <div class="meter mem-meter" title="已用显存占比"><span class="live-fill" style="width:${memPct.toFixed(1)}%"></span></div>
          </div>
        </div>
      </div>`;
    }).join('');
    const regionPeak = Number(node.peak_utilization_pct ?? 0);
    const peakHint = regionPeak > 0 ? ` · 会话峰 ${Math.round(regionPeak)}%` : '';
    return `<div class="node-card">
      <div class="node-top"><div class="region">${esc(node.region)}</div><span class="node-status ${online?'online':'offline'}">${reach}</span></div>
      <div class="node-meta">${esc(node.model)} · RTT ${node.rtt_ms ?? '—'} ms · 成本 ${Number(node.cost||0).toFixed(2)} · 可用 ${Number(node.free_gb||0).toFixed(1)} GB${peakHint}${node.agent_url? ' · '+esc(node.agent_url):''}${node.last_error? ' · '+esc(node.last_error):''}</div>
      <div class="gpu-list">${gpuHtml || '<div class="gpu"><div class="gpu-line"><span>无 GPU 信息</span><span>—</span></div></div>'}</div>
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
    {color: 'rgba(85,166,255,0.35)', data: pts.map(p => p.hainan_peak_util_pct || 0)},
    {color: 'rgba(255,180,84,0.35)', data: pts.map(p => p.chongqing_peak_util_pct || 0)},
  ], {min: 0, max: 100, digits: 0});
  const taskMax = Math.max(4, ...pts.map(p => Math.max(p.running || 0, p.queued || 0, p.succeeded_shards || 0)));
  drawLineChart($('chartTasks'), [
    {color: '#55a6ff', data: pts.map(p => p.running)},
    {color: '#ffb454', data: pts.map(p => p.queued)},
    {color: '#36d399', data: pts.map(p => p.succeeded_shards)},
  ], {min: 0, max: taskMax, digits: 0});
}

function drawBarChart(canvas, bars) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 360;
  const cssH = canvas.clientHeight || 200;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = cssW, H = cssH;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = 'rgba(7,18,32,0.55)';
  ctx.fillRect(0, 0, W, H);
  if (!bars || !bars.length) {
    ctx.fillStyle = '#7f98b2';
    ctx.font = '13px sans-serif';
    ctx.fillText('等待测试结果…', 24, H / 2);
    return;
  }
  const pad = {l: 18, r: 18, t: 24, b: 36};
  const maxV = Math.max(...bars.map(b => Number(b.value) || 0), 1);
  const slot = (W - pad.l - pad.r) / bars.length;
  bars.forEach((b, i) => {
    const v = Number(b.value) || 0;
    const bh = ((H - pad.t - pad.b) * v) / maxV;
    const x = pad.l + i * slot + slot * 0.18;
    const y = H - pad.b - bh;
    const w = slot * 0.64;
    ctx.fillStyle = b.color || '#55a6ff';
    ctx.fillRect(x, y, w, Math.max(2, bh));
    ctx.fillStyle = '#d7e6f5';
    ctx.font = '11px ui-monospace, monospace';
    ctx.fillText(`${v}${b.unit || ''}`, x, y - 8);
    ctx.fillStyle = '#8ea3bb';
    ctx.font = '10px sans-serif';
    ctx.fillText(b.label || '', x, H - 12);
  });
}

function renderOutcome(data) {
  const outcome = data.last_outcome || (data.outcomes && data.outcomes[0]) || null;
  const hist = data.outcomes || [];
  if (!outcome) {
    $('outcomeHeadline').textContent = '尚未完成测试';
    $('outcomeBullets').innerHTML = '<div class="outcome-empty">提交并完成任务后，将展示相对单边基线的节省、落点分布与峰值利用率。</div>';
    $('outcomeMeta').textContent = '跑完一个场景后这里会汇总成本/时延/利用率收益';
    $('outcomePeak').textContent = `会话峰值利用率：海南 ${(data.peak_util||{}).海南 || 0}% / 重庆 ${(data.peak_util||{}).重庆 || 0}%`;
    drawBarChart($('chartOutcome'), []);
  } else {
    $('outcomeHeadline').textContent = outcome.headline || '测试完成';
    $('outcomeMeta').textContent = `${outcome.finished_at || ''} · ${outcome.parent_id || ''}`;
    $('outcomeBullets').innerHTML = (outcome.bullets || []).map(t => `<div class="bullet">• ${esc(t)}</div>`).join('');
    const peak = outcome.peak_util || {};
    $('outcomePeak').textContent = `本轮峰值利用率：海南 ${peak['海南'] ?? 0}% / 重庆 ${peak['重庆'] ?? 0}%　｜　成本↓${outcome.cost_saving_pct ?? 0}%　时延↓${outcome.latency_improve_pct ?? 0}%`;
    drawBarChart($('chartOutcome'), outcome.bars || []);
  }
  $('outcomeHistory').innerHTML = hist.map((o, idx) =>
    `<button class="outcome-chip ${idx===0?'active':''}" data-idx="${idx}">${esc(o.scenario_code || o.scenario || 'RUN')} ${esc(String(o.success_rate_pct ?? ''))}% · ↓${esc(String(o.cost_saving_pct ?? 0))}%</button>`
  ).join('');
  $('outcomeHistory').querySelectorAll('.outcome-chip').forEach(btn => {
    btn.onclick = () => {
      const o = hist[Number(btn.dataset.idx)];
      if (!o) return;
      $('outcomeHeadline').textContent = o.headline || '';
      $('outcomeBullets').innerHTML = (o.bullets || []).map(t => `<div class="bullet">• ${esc(t)}</div>`).join('');
      drawBarChart($('chartOutcome'), o.bars || []);
      $('outcomeHistory').querySelectorAll('.outcome-chip').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
    };
  });
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
    renderOutcome(data);
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
window.addEventListener('resize', () => {
  if (!last) return;
  renderCharts(last.metrics || []);
  renderOutcome(last);
});
refresh();
setInterval(refresh, 1000);
