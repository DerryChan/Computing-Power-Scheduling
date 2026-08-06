const $ = (id) => document.getElementById(id);
let last = null;
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
  $('statQueue').textContent = `${s.QUEUED || 0} / ${(s['FAILED-LINK'] || 0) + (s.UNSCHEDULED || 0)}`;
  $('statRate').textContent = s.total ? `${((s.succeeded || 0) / s.total * 100).toFixed(1)}%` : '—';
  $('lastUpdate').textContent = `更新于 ${time(data.server_time)}`;
  $('modeBadge').textContent = String(data.mode || 'SIMULATION').toUpperCase();
}
function renderNodes(nodes) {
  $('nodes').innerHTML = nodes.map(node => {
    const online = node.healthy && node.link_up;
    return `<div class="node-card">
      <div class="node-top"><div class="region">${esc(node.region)}</div><span class="node-status ${online?'online':'offline'}">${online?'● ONLINE':'● OFFLINE'}</span></div>
      <div class="node-meta">${esc(node.model)} · RTT ${node.rtt_ms} ms · 成本 ${node.cost.toFixed(2)} · 可用 ${node.free_gb.toFixed(1)} GB</div>
      ${node.gpus.map(gpu => `<div class="gpu"><div class="gpu-line"><span>${esc(gpu.id)}</span><span>${gpu.free_gb.toFixed(1)} / ${gpu.total_gb} GB free</span></div><div class="meter"><i style="width:${Math.min(100,gpu.load_pct)}%"></i></div></div>`).join('')}
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
    const id = isParent ? task.task_id : task.task_id;
    const region = task.selected_region || (isParent ? (task.regions || []).join(' / ') : '—');
    const result = task.result_sha256 ? `<span class="hash">${esc(task.result_sha256.slice(0,18))}…</span>` : `<span class="reason">${esc(task.message || task.reason || '—')}</span>`;
    return `<tr><td><div class="task-id">${esc(id)}</div>${isParent?'':'<div class="task-child">父任务 '+esc(task.parent_id)+'</div>'}</td><td>${isParent?'PARENT':'SHARD S'+String(task.shard).padStart(2,'0')}</td><td>${statusTag(task.status)}</td><td>${esc(region)}<br><span class="task-child">${esc(task.gpu_id || '—')}</span></td><td><div class="progress"><div class="meter"><i style="width:${task.progress||0}%"></i></div><em>${Math.round(task.progress||0)}%</em></div></td><td>${result}</td></tr>`;
  }).join('');
}
function renderEvents(events) {
  $('events').innerHTML = events.length ? events.slice(0,35).map(e => `<div class="event"><time>${time(e.ts)}</time><div><b>${esc(e.event)} <span class="task-child">${esc(e.task_id)}</span></b><small>${esc(e.message || '')}</small></div></div>`).join('') : '<div class="empty">暂无事件</div>';
}
async function refresh() {
  try { const data = await api('/api/status'); last = data; renderStats(data); renderNodes(data.nodes || []); renderTasks(data.tasks || []); renderEvents(data.events || []); setConnection(true, '已连接'); }
  catch (error) { setConnection(false, '连接失败'); console.warn(error); }
}
async function toggleNode(region, field) { try { await api('/api/nodes/toggle',{method:'POST',body:JSON.stringify({region,field})}); await refresh(); } catch(e) { alert(e.message); } }
window.toggleNode = toggleNode;

$('scenario').addEventListener('change', () => {
  const value = $('scenario').value;
  const hints = {cross_region:'两地分片：S01/S02固定海南，S03/S04固定重庆，其余分片自动调度。',vram16:'单卡16GB任务：重庆12GB单卡候选阶段直接排除，只能选择海南。',otn_outage:'执行到重庆分片时模拟OTN中断：重庆停派，失败分片关联重试至海南。',normal:'普通任务按选择策略实时选择满足硬约束的节点。'};
  $('scenarioHint').textContent = hints[value] || '';
  if (value === 'vram16') $('memory').value = 16; else $('memory').value = 8;
  if (value === 'normal') $('shards').value = 1; else $('shards').value = 8;
});
$('taskForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]'); button.disabled = true;
  try { await api('/api/tasks',{method:'POST',body:JSON.stringify({task_id:$('taskId').value.trim()||undefined,scenario:$('scenario').value,shards:Number($('shards').value),memory_gb:Number($('memory').value),mode:$('mode').value})}); $('taskId').value=''; await refresh(); }
  catch(e) { alert(e.message); }
  finally { button.disabled = false; }
});
$('resetBtn').addEventListener('click', async () => { if (confirm('确定清空当前仿真任务和事件吗？')) { await api('/api/reset',{method:'POST',body:'{}'}); await refresh(); } });
refresh(); setInterval(refresh, 1000);
