# 跨境算力调度 PoC（Computing Power Scheduling）

新加坡控制面 + 海南/重庆真实 GPU 节点的 L1 跨境算力调度验证项目。主业务为冻结 ResNet-50 分片推理。

## 目录结构

```
docs/          测试实施方案等依据文档
src/           真实调度源码（控制面 / 节点代理 / PoC bundle）
  controller/  新加坡控制面与 UI
  agent/       海南、重庆 node agent
  poc_bundle/  推理与汇聚脚本
reports/       正式测试报告、图表、证据包
scripts/       报告生成等工具脚本
archive/       历史仿真材料（只读归档，不参与当前部署）
```

## 快速说明

| 角色 | 说明 |
|------|------|
| 控制面 | `src/controller/server.py` |
| 节点代理 | `src/agent/node_agent.py` |
| 推理脚本 | `src/poc_bundle/app/infer.py` |
| 正式报告 | `reports/L1_算力调度真实调度测试报告_20260811.docx` |

重新生成报告：

```bash
python3 scripts/generate_report.py
```

## 安全

服务器密码、Token 等敏感信息保存在本地 `.local/`（已 gitignore），不进入版本库。
