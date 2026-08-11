# L1 算力调度实时可视化部署说明

## 当前能力

这是一个零第三方依赖的 Python 标准库服务，默认提供 `simulation` 模式：

- 提交两地分片任务，实时显示父任务、子任务、调度候选集和选择原因；
- 海南优先、最小延迟、最小成本、加权平均四种策略；
- 16GB 单卡显存任务自动排除重庆 12GB 单卡；
- 模拟重庆 OTN 中断，展示停派、失败、关联重试至海南和链路恢复；
- 通过结果 SHA-256、事件流和任务ID展示结果汇聚与审计过程；
- 可在页面上切换节点健康状态和链路状态，观察调度变化。

当前服务没有接入真实海南/重庆 GPU、VPN/OTN 或现有调度 API，所以页面中的 `PASS-SIM`/实时结果只能作为联调演示。要做真实服务器测试，需要把 `SchedulerState.choose()` 和任务执行线程替换为现有调度平台 API 或节点代理调用，并保留同样的事件字段。

## 本地启动

```bash
cd webapp
SCHEDULER_UI_TOKEN='replace-with-a-long-random-token' python3 server.py --host 0.0.0.0 --port 8080
```

浏览器访问 `http://127.0.0.1:8080/`。如果配置了令牌，在页面顶部“访问令牌”输入同一个值。

## 新加坡服务器部署

下面命令假定已经可以 SSH 登录新加坡服务器，并把本目录上传到 `/opt/l1-scheduler-ui`。当前工作环境到服务器 `43.106.50.98:22` 的网络路由不可用，因此本次只生成部署包，没有远程执行安装。

```bash
sudo mkdir -p /opt/l1-scheduler-ui
sudo cp -r webapp/. /opt/l1-scheduler-ui/
sudo chmod 755 /opt/l1-scheduler-ui/server.py

# 生成令牌并写入 systemd 文件中的 Environment 行
openssl rand -hex 32
sudo cp /opt/l1-scheduler-ui/systemd/l1-scheduler-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now l1-scheduler-ui
sudo systemctl status l1-scheduler-ui --no-pager
```

对外端口默认是 `8080`。还需要在云厂商安全组和系统防火墙开放 TCP 8080：

```bash
sudo ufw allow 8080/tcp
curl http://127.0.0.1:8080/api/health
```

正式使用建议放在 Nginx/Caddy 后面，以 HTTPS 对外提供，并将 8080 限制为本机访问；如果仅做受控演示，至少保留 `SCHEDULER_UI_TOKEN`，不要把令牌写进前端代码或 URL。

## 常用接口

```text
GET  /api/health
GET  /api/status
POST /api/tasks
POST /api/nodes/toggle   {"region":"重庆","field":"link_up"}
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
