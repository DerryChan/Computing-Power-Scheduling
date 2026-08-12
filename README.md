# 跨境算力调度（Computing Power Scheduling）

自适应动态权重多目标调度引擎，衔接新加坡控制面与海南/重庆真实 GPU。

## 算法要点

1. **硬约束过滤**：GPU 剩余容量、时延上限、预算上限、TEE 合规  
2. **最小-最大规范化**：对时延 / 成本 / 能耗在可行集上归一化  
3. **自适应打分**：

```
Score_i = wl * S(t) * N_latency + wc * N_cost + we * N_energy + wld * Load_i
```

默认权重：`wl=0.733, wc=0.1, we=0.1, wld=0.1`

## 目录结构

```
docs/                 PoC 测试实施方案
src/
  scheduler/          调度核心、基线、30 任务实验
  controller/         控制面 API + UI
  agent/              海南、重庆 node agent
  poc_bundle/         ResNet 推理与汇聚
reports/              测试报告、图表、实验数据
scripts/              generate_report.py
tests/                单元测试
```

## 快速验证

```bash
# 单元测试
PYTHONPATH=src python3 -m unittest tests.test_scheduler -v

# 30 任务对比实验
PYTHONPATH=src python3 -c "from scheduler.experiment import run_paper_experiment, assert_experiment_health; \
p=run_paper_experiment('reports/data'); print(p['summaries']['本文方法（动态权重多目标调度）']); print(assert_experiment_health(p))"

# 生成 Word 测试报告
python3 scripts/generate_report.py

# 启动控制面
cd src/controller && PYTHONPATH=.. SCHEDULER_FABRIC=hybrid python3 server.py
```

正式报告：

- `reports/跨境算力调度算法测试报告.docx`
- `reports/跨境算力调度真实联调测试报告.docx`

## API

| 路径 | 说明 |
|------|------|
| `GET/POST /api/paper/experiment` | 跑/读取 30 任务对比实验 |
| `GET /api/paper/nodes` | 五节点资源模型 |
| `GET /api/status` | 含 `algorithm` / `last_decision` / `fabric` |

默认调度策略：`动态权重多目标`（兼容海南优先 / 最小延迟 / 最小成本等）。

## 安全

服务器密码、Token 等保存在本地 `.local/`（已 gitignore），不进入版本库。
