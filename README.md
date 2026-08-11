# Computing-Power-Scheduling

跨境算力调度 PoC：新加坡控制面 + 海南/重庆真实 GPU 节点（ResNet-50 分片推理）。

## 目录

- `webapp/real/`：真实调度控制面、node agent、PoC bundle 脚本
- `webapp/`：早期仿真 UI（历史）
- `deploy/`：部署安装脚本
- `reports/`：测试报告（Word）、图表与证据包

## 真实调度快速说明

| 角色 | 地址 | 端口 |
|------|------|------|
| 新加坡控制面 UI | `43.106.50.98` | 8080 |
| 海南 agent | 经隧道 `127.0.0.1:18000` | 8000 |
| 重庆 agent | `218.201.8.129` | 8000 |

源码入口：
- 控制面：`webapp/real/controller/server.py`
- 节点代理：`webapp/real/agent/node_agent.py`

## 报告

正式真实调度报告（仅通过用例）：`reports/L1_算力调度真实调度测试报告_20260811.docx`

## 安全

仓库不包含服务器密码、私钥或 Token。请使用本地安全渠道保管凭据。
