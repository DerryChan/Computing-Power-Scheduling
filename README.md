# Computing Power Scheduling

跨区域算力调度 PoC 的实时可视化前后端。当前版本提供一个零第三方依赖的 Python 服务和浏览器控制台，用于验证海南/重庆两地资源调度、分片执行、显存硬约束、链路故障重试、结果汇聚与审计事件流。

## 当前版本

- 运行模式：`simulation`，用于实时联调演示和调度规则验证。
- 前端：`webapp/static/`，展示节点、GPU、任务、分片、事件和结果哈希。
- 后端：`webapp/server.py`，仅依赖 Python 3.9+ 标准库。
- 默认端口：`8080`。
- 认证：通过环境变量 `SCHEDULER_UI_TOKEN` 启用 Bearer Token。

当前版本不会访问真实 GPU、VPN/OTN 或既有调度 API。接入真实节点时，应在后端保留相同的任务状态和事件字段，再替换调度决策与执行适配器。

## 本地运行

```bash
SCHEDULER_UI_TOKEN='replace-with-a-long-random-token' \
python3 webapp/server.py --host 127.0.0.1 --port 8080
```

浏览器打开 `http://127.0.0.1:8080/`，并在页面顶部输入同一个令牌。

## 可演示场景

1. 两地分片推理：固定部分分片到海南和重庆，其余按策略选择。
2. 单卡 16GB 显存约束：重庆 RTX 4070 12GB 自动被排除。
3. 重庆链路中断：停止向重庆派发，失败分片关联重试至海南，随后恢复链路。
4. 节点健康与链路切换：在页面上手动切换节点状态，观察候选集变化。
5. 结果汇聚：每个分片生成 SHA-256，父任务完成后生成汇聚哈希。

## API

```text
GET  /api/health
GET  /api/status
POST /api/tasks
POST /api/nodes/toggle
POST /api/reset
```

提交示例：

```json
{
  "task_id": "POC-LIVE-001",
  "scenario": "cross_region",
  "shards": 8,
  "memory_gb": 8,
  "mode": "海南优先"
}
```

## 新加坡服务器部署

推荐从本仓库部署到 `/opt/Computing-Power-Scheduling`，然后使用 `deploy/l1-scheduler-ui.service`。详细步骤见 [`webapp/README_DEPLOY.md`](webapp/README_DEPLOY.md)。公网开放前必须配置随机令牌，并在云安全组和系统防火墙中仅开放需要的端口。

## 后续接入真实算力

现场验收仍需补充真实 GPU 资源采集、调度平台 API/节点代理、VPN/OTN 链路、镜像执行、结果回传和清理适配。仿真页面的 PASS 结果不等同于真实环境验收结果。

