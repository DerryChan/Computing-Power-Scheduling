#!/usr/bin/env python3
"""L1 real scheduler control plane (Singapore).

Polls Hainan/Chongqing node agents for real GPU state, dispatches ResNet shard
tasks over HTTP, collects predictions evidence, and exposes a UI-compatible API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
SRC_ROOT = ROOT.parent
STATIC = ROOT / "static"
TOKEN = os.environ.get("SCHEDULER_UI_TOKEN", "").strip()
AGENT_TOKEN = os.environ.get("L1_AGENT_TOKEN", TOKEN).strip()
MODE = os.environ.get("SCHEDULER_MODE", "real")
PYTHON = os.environ.get("L1_PYTHON", "python3")
FABRIC = os.environ.get("SCHEDULER_FABRIC", "hybrid").strip().lower()  # hybrid|real|paper

# 本地开发：ROOT=src/controller，SRC_ROOT=src
# 线上扁平部署：ROOT=/opt/l1-scheduler-ui，scheduler/ 与 scheduler_bridge.py 同目录
for _p in (str(ROOT), str(SRC_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from controller.scheduler_bridge import choose_with_paper, paper_fabric_nodes  # type: ignore
except ImportError:
    from scheduler_bridge import choose_with_paper, paper_fabric_nodes  # type: ignore
from scheduler.experiment import run_paper_experiment
from scheduler.model import DEFAULT_WEIGHTS, make_paper_nodes

HAINAN_URL = os.environ.get("L1_HAINAN_URL", "http://127.0.0.1:18000").rstrip("/")
CHONGQING_URL = os.environ.get("L1_CHONGQING_URL", "http://218.201.8.129:8000").rstrip("/")
BUNDLE = Path(os.environ.get("L1_POC_BUNDLE", "/opt/l1-poc-bundle"))
DATA_ROOT = BUNDLE / "data" / "dataset-140m"
MERGE_SCRIPT = BUNDLE / "app" / "merge_results.py"
REFERENCE_PRED = BUNDLE / "reference" / "predictions_all.jsonl"
if not REFERENCE_PRED.is_file():
    REFERENCE_PRED = BUNDLE / "reference" / "predictions.jsonl"
EVIDENCE_ROOT = Path(os.environ.get("L1_EVIDENCE_ROOT", "/opt/l1-scheduler-ui/evidence"))
CASE_EVIDENCE_DIR = EVIDENCE_ROOT / "case_evidence"
AUDIT_LOG = EVIDENCE_ROOT / "audit_log.jsonl"

RESNET_SCENARIOS = frozenset({
    "dt01_cross_region", "dt02_divert", "dt03_vram16", "dt04_otn_outage", "dt05_isolation",
    "tc01_ingress", "tc12_backpressure", "tc17_cancel", "tc18_timeout", "tc20_return",
    "tc22_node_offline", "tc24_vpn_down", "tc28_cleanup", "tc29_audit", "tc30_stability_short", "normal",
})

SCENARIOS: dict[str, dict[str, Any]] = {
    "dt01_cross_region": {
        "id": "dt01_cross_region", "code": "DT01", "level": "A",
        "title": "两地共同执行的分片批量推理",
        "summary": "父任务拆成 8 个 ResNet 分片，两地并行；推理后短时占卡，便于观察利用率。",
        "detail": (
            "对应报告 DT01 / TC13 / TC14 / TC15。\n"
            "真实调度：S01/S02 亲和海南，S03/S04 亲和重庆，其余按策略自动选择；"
            "每个分片先跑 ResNet（通常 1–3 秒），再按 duration_sec 补一段 GEMM 占卡（最多约 15 秒）便于观察利用率；"
            "完成后从 agent 拉取 predictions.jsonl 并在控制面 merge。\n"
            "观察：两地利用率条/峰值、分片落点、合并结果 SHA。\n"
            "若只要更长占卡，可选场景 DEMO（gpu_load ≈25 秒）。"
        ),
        "defaults": {"shards": 8, "memory_gb": 8, "mode": "动态权重多目标", "duration_sec": 18},
    },
    "dt02_divert": {
        "id": "dt02_divert", "code": "DT02", "level": "A",
        "title": "海南优先与重庆自动分流",
        "summary": "空闲时选海南；用 gpu_load 占满海南单卡显存后，同等 ResNet 任务分流重庆。",
        "detail": (
            "对应报告 DT02 / TC08 / TC09。\n"
            "真实调度：第 1 分片应落海南；随后在海南 4090 上启动 gpu_load 占位"
            "（约 20GB，若空闲不足则占 free-1.5GB）；第 2 分片因单卡可用显存不足改派重庆。\n"
            "硬件说明：海南现场当前为 1×RTX 4090。"
        ),
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "dt03_vram16": {
        "id": "dt03_vram16", "code": "DT03", "level": "A",
        "title": "单卡 16GB 显存硬约束",
        "summary": "16GB ResNet 任务在候选阶段排除重庆 12GB 卡，仅派海南 4090。",
        "detail": (
            "对应报告 DT03 / TC10。\n"
            "真实调度：按 agent 回报的单卡 free/total 过滤；重庆 4070 不得被当成统一显存池；"
            "不得先派重庆再 OOM。\n"
            "观察：重庆出现在 rejected；任务只调度到海南。"
        ),
        "defaults": {"shards": 1, "memory_gb": 16, "mode": "海南优先", "duration_sec": 30},
    },
    "dt04_otn_outage": {
        "id": "dt04_otn_outage", "code": "DT04", "level": "A",
        "title": "重庆链路中断、停派与分片重试",
        "summary": "重庆亲和分片到达时断开重庆 agent 链路，失败后重试到海南并恢复链路。",
        "detail": (
            "对应报告 DT04 / TC23 / TC25。\n"
            "真实调度：调用重庆 agent /v1/admin/toggle link_up；停派后重试海南；"
            "结束恢复链路并汇聚 ResNet 分片结果。\n"
            "观察：LINK_DOWN、FAILED-LINK→RETRYING、父任务最终仍可成功。"
        ),
        "defaults": {"shards": 8, "memory_gb": 8, "mode": "动态权重多目标", "duration_sec": 30},
    },
    "dt05_isolation": {
        "id": "dt05_isolation", "code": "DT05", "level": "A",
        "title": "结果汇聚、跨境回传与数据清理",
        "summary": "并行 A/B 父任务，验证 ResNet 结果键隔离、哈希分离与 agent 清理留痕。",
        "detail": (
            "对应报告 DT05 / TC20 / TC21 / TC28。\n"
            "真实调度：同时创建父任务 A 与 B，各自 4 个 ResNet 分片、独立 evidence 目录；"
            "汇聚后分别生成 result SHA-256；确认后触发 agent cleanup 并写清理记录。\n"
            "观察：A/B 结果哈希不同且不交叉；事件流出现 CLEANUP / RETURN。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc01_ingress": {
        "id": "tc01_ingress", "code": "TC01", "level": "A",
        "title": "正常跨境接入与完整性校验",
        "summary": "登记前校验 input_manifest.json / SHA256SUMS，通过后真实下发 ResNet 分片。",
        "detail": (
            "对应报告 TC01 / TC04 / TC29。\n"
            "操作含义：冻结 bundle 的 input_manifest.json 与 SHA256SUMS 全部通过才登记；"
            "随后进入分片调度并写 SUBMIT / DISPATCH / COMPLETE 审计。\n"
            "观察：审计字段含任务 ID、阶段、节点、时间戳。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc02_auth_fail": {
        "id": "tc02_auth_fail", "code": "TC02", "level": "A",
        "title": "身份认证失败",
        "summary": "模拟错误 Bearer 访问 agent，记录认证失败审计，不创建可执行 GPU 任务。",
        "detail": (
            "对应报告 TC02。\n"
            "UI 层错误 Authorization 已由 HTTP handler 拒绝；本场景额外向 agent 发送错误 token，"
            "写入 AUTH_FAIL 审计事件，父任务状态 REJECTED，不产生 RUNNING 分片。\n"
            "观察：audit_log.jsonl 有认证失败记录；children 为空或全部为 REJECTED。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 5},
    },
    "tc03_unauth_port": {
        "id": "tc03_unauth_port", "code": "TC03", "level": "A",
        "title": "非授权端口或路径",
        "summary": "探测本机关闭端口/非授权路径，记录 blocked 审计，不创建可执行任务。",
        "detail": (
            "对应报告 TC03。\n"
            "操作含义：向本地关闭端口发起连接探针，模拟访问未开放管理口；"
            "记录 PROBE_BLOCKED 与审计事件，拒绝任务创建。\n"
            "观察：连接被拒绝/超时；无 GPU 占用。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 5},
    },
    "tc04_hash_mismatch": {
        "id": "tc04_hash_mismatch", "code": "TC04", "level": "A",
        "title": "数据完整性异常",
        "summary": "故意提交与冻结值不一致的数据集哈希，登记阶段拒绝，不创建可执行任务。",
        "detail": (
            "对应报告 TC04。\n"
            "操作含义：控制面在 start_task 时校验 dataset_tar_sha256；"
            "本场景注入错误哈希，校验失败后写 HASH_MISMATCH 审计并返回 400。\n"
            "观察：错误哈希任务创建数为 0。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 5},
    },
    "tc06_resource_discover": {
        "id": "tc06_resource_discover", "code": "TC06", "level": "A",
        "title": "资源发现准确性快照",
        "summary": "从两地 agent 拉取真实 GPU 快照并写入审计，不跑 ResNet 重计算。",
        "detail": (
            "对应报告 TC06 / TC07。\n"
            "操作含义：调用 /v1/resources，核对型号、数量、单卡显存、健康、链路字段；"
            "写入 RESOURCE_SNAPSHOT 事件。\n"
            "观察：海南当前 1×4090；重庆 GPU 列表完整。"
        ),
        "defaults": {"shards": 1, "memory_gb": 1, "mode": "海南优先", "duration_sec": 1},
    },
    "tc12_backpressure": {
        "id": "tc12_backpressure", "code": "TC12", "level": "B",
        "title": "资源耗尽与背压",
        "summary": "用 gpu_load 占满两地 GPU 后再提交 ResNet，任务应 UNSCHEDULED/排队且服务不崩溃。",
        "detail": (
            "对应报告 TC12。\n"
            "真实调度：先向所有空闲 GPU 下发占位 gpu_load，再提交检测 ResNet 分片；"
            "系统返回无候选或排队，控制面保持可查询。\n"
            "观察：UNSCHEDULED/QUEUED；服务未崩溃。"
        ),
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "海南优先", "duration_sec": 20},
    },
    "tc17_cancel": {
        "id": "tc17_cancel", "code": "TC17", "level": "B",
        "title": "任务取消",
        "summary": "启动长时 gpu_load/ResNet 分片后调用 agent cancel，状态转为 CANCELLED。",
        "detail": (
            "对应报告 TC17。\n"
            "真实调度：下发长时 gpu_load 或 ResNet 分片，进入 RUNNING 后 POST /v1/tasks/{id}/cancel；"
            "验证不再产生 SUCCEEDED 记录。\n"
            "观察：CANCELLED 状态与取消审计事件。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 120},
    },
    "tc18_timeout": {
        "id": "tc18_timeout", "code": "TC18", "level": "A",
        "title": "超时及执行失败分类",
        "summary": "向 agent 下发 force_fail，验证 FAILED-TIMEOUT 分类留痕。",
        "detail": (
            "对应报告 TC18。\n"
            "真实路径调用 agent，任务被标记 force_fail，进入 FAILED-TIMEOUT；"
            "父任务记录失败分片数，错误原因可查询。\n"
            "观察：状态不是假成功。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 10},
    },
    "tc20_return": {
        "id": "tc20_return", "code": "TC20", "level": "A",
        "title": "结果回传完整性",
        "summary": "ResNet 分片完成后拉取 predictions.jsonl、merge 并复算父结果哈希。",
        "detail": (
            "对应报告 TC20。\n"
            "真实调度：每个成功分片从 agent GET predictions.jsonl 存至 evidence/{parent}/；"
            "父任务完成后 merge_results.py 汇聚并对比 reference（若存在）。\n"
            "观察：result_sha256 长度 64；merged summary.json 可查询。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc22_node_offline": {
        "id": "tc22_node_offline", "code": "TC22", "level": "A",
        "title": "计算节点离线停派",
        "summary": "将重庆 agent 置 unhealthy，验证不再向其派发 ResNet 分片。",
        "detail": (
            "对应报告 TC22 / TC26。\n"
            "真实调度：toggle healthy；提交可调度到两地的任务；重庆不得进入候选；"
            "结束后恢复 healthy。\n"
            "观察：rejected 原因为节点不健康。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc24_vpn_down": {
        "id": "tc24_vpn_down", "code": "TC24", "level": "A",
        "title": "新加坡—海南 VPN 中断",
        "summary": "临时将海南 agent 视为不可达，停派后恢复 VPN 并继续调度。",
        "detail": (
            "对应报告 TC24。\n"
            "真实调度：设置内部 hainan_unreachable 标志模拟 VPN 中断；"
            "海南分片失败或改派；恢复标志后重新纳管海南。\n"
            "观察：VPN_DOWN / VPN_RECOVERED 事件；海南 reachable 恢复。"
        ),
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc27_bad_image": {
        "id": "tc27_bad_image", "code": "TC27", "level": "A",
        "title": "镜像摘要不一致拒绝调度",
        "summary": "agent 侧拒绝 bad_image 任务，不占用 GPU。",
        "detail": (
            "对应报告 TC27 / TC11。\n"
            "真实调用 agent 并传 bad_image=true，返回 FAILED/UNSCHEDULED；"
            "保留失败原因，不进入 RUNNING。\n"
            "观察：无 GPU 占用；镜像校验失败事件。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 5},
    },
    "tc28_cleanup": {
        "id": "tc28_cleanup", "code": "TC28", "level": "A",
        "title": "任务完成后数据清理",
        "summary": "ResNet 分片成功后触发 agent cleanup，保留审计日志。",
        "detail": (
            "对应报告 TC28。\n"
            "真实调度：完成 1 个 ResNet 分片并确认结果后 POST /v1/cleanup；"
            "记录清理对象与 removed_count。\n"
            "观察：cleanup_record 写入父任务与 case evidence。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc29_audit": {
        "id": "tc29_audit", "code": "TC29", "level": "A",
        "title": "全流程审计追踪",
        "summary": "执行精简 ResNet 父任务，验证 SUBMIT→DISPATCH→COMPLETE 审计链完整。",
        "detail": (
            "对应报告 TC29。\n"
            "真实调度：2 个 ResNet 分片；全过程写 audit_log.jsonl，"
            "字段含 task_id、event、ts、region、reason。\n"
            "观察：关键字段完整率 100%，时间顺序可关联。"
        ),
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "海南优先", "duration_sec": 30},
    },
    "tc30_stability_short": {
        "id": "tc30_stability_short", "code": "TC30", "level": "A/C",
        "title": "连续稳定运行（短程）",
        "summary": "短程连续多轮 ResNet 分片，检查状态完整性与结果隔离（非 24h 现场替代）。",
        "detail": (
            "对应报告 TC30 短程版。\n"
            "真实调度：连续 6 个 ResNet 分片依次下发，记录每轮成功/失败；"
            "未宣称替代现场 24 小时长跑。\n"
            "观察：success_rate、metrics 历史连续无崩溃。"
        ),
        "defaults": {"shards": 6, "memory_gb": 8, "mode": "海南优先", "duration_sec": 20},
    },
    "demo_util": {
        "id": "demo_util", "code": "DEMO", "level": "A",
        "title": "利用率与收益可视化演示",
        "summary": "两地并行 gpu_load 占位约 25 秒，便于看清利用率爬升，并在结束后展示成本/时延收益摘要。",
        "detail": (
            "专门用于演示：ResNet 推理仅 1–2 秒，瞬时利用率容易被 1 秒采样漏掉；\n"
            "本场景改用 gpu_load 持续占显存，折线与峰值更明显；完成后右侧/下方收益面板会给出相对单边基线的节省。"
        ),
        "defaults": {"shards": 4, "memory_gb": 10, "mode": "动态权重多目标", "duration_sec": 25},
    },
    "normal": {
        "id": "normal", "code": "CUSTOM", "level": "—",
        "title": "自定义真实 ResNet 调度",
        "summary": "按策略把 ResNet 分片派到真实节点，可配合节点离线/断链按钮。",
        "detail": "自由测试入口：可改分片数、显存和策略；默认 workload=resnet。",
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "动态权重多目标", "duration_sec": 30},
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 8.0,
    token: str | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    auth = token if token is not None else AGENT_TOKEN
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"error": body or str(exc)}
        parsed["_http_status"] = exc.code
        return parsed
    except Exception as exc:
        return {"error": str(exc), "_unreachable": True}


def http_fetch_bytes(url: str, timeout: float = 30.0, token: str | None = None) -> bytes | None:
    headers = {"Accept": "application/octet-stream"}
    auth = token if token is not None else AGENT_TOKEN
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def verify_bundle_integrity(expect_tar_hash: str | None = None) -> dict[str, Any]:
    """Verify input_manifest.json and SHA256SUMS against frozen bundle files."""
    manifest_path = BUNDLE / "input_manifest.json"
    sums_path = BUNDLE / "SHA256SUMS"
    result: dict[str, Any] = {
        "ok": False,
        "bundle": str(BUNDLE),
        "manifest_exists": manifest_path.is_file(),
        "sums_exists": sums_path.is_file(),
        "checked": [],
        "errors": [],
    }
    if not manifest_path.is_file():
        result["errors"].append("input_manifest.json missing")
        return result
    if not sums_path.is_file():
        result["errors"].append("SHA256SUMS missing")
        return result

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result["manifest"] = manifest
    expected_entries: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, rel = line.split(None, 1)
        expected_entries[rel.strip()] = digest.strip()

    for rel, expected in sorted(expected_entries.items()):
        path = BUNDLE / rel
        if not path.is_file():
            result["errors"].append(f"missing file: {rel}")
            continue
        actual = sha256_file(path)
        ok = actual == expected
        result["checked"].append({"path": rel, "expected": expected, "actual": actual, "ok": ok})
        if not ok:
            result["errors"].append(f"hash mismatch: {rel}")

    frozen_tar = manifest.get("dataset_tar_sha256", "")
    if expect_tar_hash is not None and expect_tar_hash != frozen_tar:
        result["errors"].append(f"dataset_tar_sha256 override mismatch: {expect_tar_hash} != {frozen_tar}")
        result["checked"].append({
            "path": "dataset_tar_sha256",
            "expected": frozen_tar,
            "actual": expect_tar_hash,
            "ok": False,
        })

    result["input_sha256"] = frozen_tar
    result["ok"] = not result["errors"]
    return result


def probe_closed_port(host: str = "127.0.0.1", port: int = 59999, timeout: float = 1.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.close()
        return {"host": host, "port": port, "blocked": False, "error": "unexpectedly connected"}
    except Exception as exc:
        return {"host": host, "port": port, "blocked": True, "error": str(exc)}


class EvidenceWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.case_dir = root / "case_evidence"

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.case_dir.mkdir(parents=True, exist_ok=True)

    def parent_dir(self, parent_id: str) -> Path:
        self._ensure_dirs()
        d = self.root / parent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def audit(self, event: str, task_id: str, message: str, **extra: Any) -> None:
        row = {"ts": now_iso(), "event": event, "task_id": task_id, "message": message, **extra}
        try:
            self._ensure_dirs()
            with open(AUDIT_LOG, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def save_predictions(self, parent_id: str, child_id: str, content: bytes) -> Path:
        path = self.parent_dir(parent_id) / f"{child_id}_predictions.jsonl"
        path.write_bytes(content)
        return path

    def merge_predictions(self, parent_id: str, parts: list[Path], expect_samples: int = 4096) -> dict[str, Any]:
        if not parts:
            return {"ok": False, "error": "no prediction parts"}
        out = self.parent_dir(parent_id) / "merged_predictions.jsonl"
        summary_path = self.parent_dir(parent_id) / "summary.json"
        if not MERGE_SCRIPT.is_file():
            combined = []
            for p in parts:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        combined.append(line)
            out.write_text("\n".join(combined) + ("\n" if combined else ""), encoding="utf-8")
            return {"ok": True, "output": str(out), "parts": len(parts), "fallback": True}

        cmd = [
            PYTHON, str(MERGE_SCRIPT),
            "--parts", *[str(p) for p in parts],
            "--output", str(out),
            "--expect-samples", str(expect_samples),
        ]
        if REFERENCE_PRED.is_file():
            cmd.extend(["--reference", str(REFERENCE_PRED)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                return {"ok": False, "error": proc.stderr or proc.stdout or "merge failed"}
            summary = json.loads(proc.stdout.strip() or "{}")
            if summary_path.is_file():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["output"] = str(out)
            return summary
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def write_case_evidence(self, parent: dict[str, Any], events: list[dict[str, Any]], merge_summary: dict[str, Any] | None) -> Path:
        code = parent.get("scenario_code") or parent.get("scenario", "").upper()
        scenario = parent.get("scenario", "")
        meta = SCENARIOS.get(scenario, {})
        parent_id = parent["task_id"]
        child_tasks = parent.get("_children", [])
        first_child = child_tasks[0]["task_id"] if child_tasks else f"{parent_id}-S01"
        integrity = parent.get("integrity_check") or {}
        doc = {
            "case_id": code if code != "CUSTOM" else "NORMAL",
            "title": meta.get("title", parent.get("scenario_title", "")),
            "level": meta.get("level", "A"),
            "status": "PASS-REAL" if parent.get("status") == "SUCCEEDED" else parent.get("status", "UNKNOWN"),
            "evidence": f"case_evidence/{code}.json",
            "detail": meta.get("summary", parent.get("message", "")),
            "metrics": parent.get("message", ""),
            "parent_task_id": parent_id,
            "child_task_id": first_child,
            "execution_time": parent.get("finished_at") or parent.get("created_at") or now_iso(),
            "preconditions": f"bundle={BUNDLE}; mode=real",
            "input_files": "dataset-140m.tar.gz; manifest.csv; manifest_part-01..08.csv",
            "input_sha256": integrity.get("input_sha256", ""),
            "shards": str(parent.get("shards", "")),
            "candidate_set": "海南/重庆；按健康、链路、VPN、镜像、兼容性和单卡显存过滤",
            "selection_reason": parent.get("message", ""),
            "execution_node": " / ".join(parent.get("regions") or []) or "现场真实节点",
            "gpu_id": ", ".join(sorted({c.get("gpu_id", "") for c in child_tasks if c.get("gpu_id")})) or "现场记录",
            "actual_outputs": f"evidence/{parent_id}/; merged_predictions.jsonl",
            "missing_samples": str((merge_summary or {}).get("missing", "NOT_APPLICABLE")),
            "duplicate_samples": str((merge_summary or {}).get("duplicates", "NOT_APPLICABLE")),
            "log_evidence": f"case_evidence/{code}.json; audit_log.jsonl",
            "issue_id": "NONE",
            "retry_relation": parent.get("retry_relation", "NONE"),
            "cleanup_record": json.dumps(parent.get("cleanup_record"), ensure_ascii=False) if parent.get("cleanup_record") else "NONE",
            "test_conclusion": f"{parent.get('status')}: {parent.get('message', '')}",
            "coverage_source": f"direct:{code}",
            "supporting_files": [
                f"evidence/{parent_id}/",
                "audit_log.jsonl",
            ],
            "result_sha256": parent.get("result_sha256", ""),
            "merge_summary": merge_summary or {},
            "events_tail": events[:20],
            "mode": MODE,
            "evidence_note": "PASS-REAL 为真实节点调度证据；失败场景保留审计留痕。",
        }
        path = self.case_dir / f"{code}.json"
        try:
            self._ensure_dirs()
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return path


class SchedulerState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.metrics: list[dict[str, Any]] = []
        self.sequence = 0
        self.agent_urls = {"海南": HAINAN_URL, "重庆": CHONGQING_URL}
        # 反向隧道偶发抖动：连续失败 N 次才标 Agent Down，避免误杀亲和分片
        self._poll_fail_streak: dict[str, int] = {"海南": 0, "重庆": 0}
        self._poll_fail_threshold = 3
        self.nodes: dict[str, dict[str, Any]] = {
            "海南": self._empty_node("海南", "RTX 4090", 15, 2.5, agent=True, green=0.70, tee=False),
            "重庆": self._empty_node("重庆", "RTX 4070", 20, 2.0, agent=True, green=0.55, tee=False),
        }
        # 混合织物中的离岸/绿电仿真节点（hybrid/paper 模式展示与算法打分）
        if FABRIC in ("hybrid", "paper"):
            for name, node in make_paper_nodes().items():
                if name in self.nodes:
                    continue
                snap = node.snapshot()
                snap.update({
                    "simulated": True,
                    "agent_url": "",
                    "reachable": True,
                    "healthy": True,
                    "link_up": True,
                    "gpus": [
                        {
                            "id": f"{name}-GPU{i+1}", "index": i, "free_gb": 24.0, "total_gb": 24.0,
                            "utilization_pct": 0, "busy": False, "model": node.model,
                        }
                        for i in range(node.gpu_capacity)
                    ],
                    "free_gb": 24.0 * node.gpu_capacity,
                    "gpu_capacity": node.gpu_capacity,
                    "gpu_free": node.gpu_free,
                    "has_tee": node.has_tee,
                    "region_tag": node.region_tag,
                })
                self.nodes[name] = snap
        self.hainan_unreachable = False
        self.evidence = EvidenceWriter(EVIDENCE_ROOT)
        self.paper_cache: dict[str, Any] | None = None
        self.last_decision: dict[str, Any] | None = None
        self.last_outcome: dict[str, Any] | None = None
        self.outcomes: list[dict[str, Any]] = []
        self._peak_util = {"海南": 0.0, "重庆": 0.0}
        self._gpu_peak_util: dict[str, float] = {}
        self._stop = False
        self.reset(soft=True)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _empty_node(
        self,
        region: str,
        model: str,
        rtt: int,
        cost: float,
        *,
        agent: bool = False,
        green: float = 0.7,
        tee: bool = False,
    ) -> dict[str, Any]:
        return {
            "region": region, "model": model, "rtt_ms": rtt, "cost": cost, "green_factor": green,
            "healthy": False, "link_up": False, "reachable": False, "gpus": [],
            "agent_url": self.agent_urls.get(region, "") if agent else "",
            "last_error": "not polled yet" if agent else "",
            "free_gb": 0.0, "simulated": not agent, "has_tee": tee,
            "gpu_capacity": 0, "gpu_free": 0,
        }

    def reset(self, soft: bool = False) -> None:
        with self.lock:
            self.tasks = {}
            self.events = []
            if not soft:
                self.metrics = []
                self.outcomes = []
                self.last_outcome = None
            self.sequence = 0
            self.hainan_unreachable = False
            self._peak_util = {"海南": 0.0, "重庆": 0.0}
            self._gpu_peak_util = {}
            self.log("SYSTEM", "RESET", "真实 ResNet 调度控制面已重置")

    def log(self, task_id: str, event: str, message: str, **extra: Any) -> None:
        item = {"ts": now_iso(), "task_id": task_id, "event": event, "message": message, **extra}
        with self.lock:
            self.events.insert(0, item)
            self.events = self.events[:300]
        self.evidence.audit(event, task_id, message, **extra)

    def _poll_loop(self) -> None:
        while not self._stop:
            self.refresh_nodes()
            self.record_metrics()
            time.sleep(1.0)

    def _agent_url(self, region: str) -> str:
        if region == "海南" and self.hainan_unreachable:
            return "__unreachable__"
        return self.agent_urls[region]

    def refresh_nodes(self) -> None:
        for region, url in self.agent_urls.items():
            if region == "海南" and self.hainan_unreachable:
                with self.lock:
                    node = self.nodes[region]
                    node.update({
                        "reachable": False,
                        "healthy": False,
                        "last_error": "VPN/simulated unreachable",
                        "gpus": [],
                        "free_gb": 0.0,
                        "gpu_free": 0,
                    })
                    self._poll_fail_streak[region] = self._poll_fail_threshold
                continue
            # 海南经反向隧道，超时略放宽；重庆直连保持适中
            timeout = 8.0 if region == "海南" else 5.0
            data = http_json("GET", f"{url}/v1/resources", timeout=timeout)
            with self.lock:
                node = self.nodes[region]
                if data.get("_unreachable") or (data.get("error") and "gpus" not in data):
                    streak = self._poll_fail_streak.get(region, 0) + 1
                    self._poll_fail_streak[region] = streak
                    err = data.get("error") or "unreachable"
                    # 未达阈值：保留上次快照，仅挂告警，不立刻 AGENT DOWN
                    if streak < self._poll_fail_threshold:
                        node["last_error"] = f"探测抖动({streak}/{self._poll_fail_threshold}): {err}"
                        continue
                    node["reachable"] = False
                    node["healthy"] = False
                    node["last_error"] = err
                    continue
                self._poll_fail_streak[region] = 0
                gpus = [g for g in (data.get("gpus") or []) if "index" in g]
                # nvidia-smi 瞬时利用率任务结束后立刻归零；按 GPU 累计会话峰值供卡片展示
                for g in gpus:
                    gid = str(g.get("id") or f"{region}-GPU{int(g.get('index', 0)) + 1}")
                    g["id"] = gid
                    cur = float(g.get("utilization_pct") or 0)
                    peak = max(float(self._gpu_peak_util.get(gid, 0) or 0), cur)
                    self._gpu_peak_util[gid] = peak
                    g["peak_utilization_pct"] = round(peak, 1)
                node.update({
                    "reachable": True,
                    "healthy": bool(data.get("healthy", True)),
                    "link_up": bool(data.get("link_up", True)),
                    "gpus": gpus,
                    "free_gb": round(sum(float(g.get("free_gb", 0)) for g in gpus), 2),
                    "gpu_capacity": max(len(gpus), 1),
                    "gpu_free": sum(1 for g in gpus if float(g.get("free_gb", 0)) >= 1.0 and not g.get("busy")),
                    "model": gpus[0]["model"] if gpus else node.get("model"),
                    "last_error": "",
                    "server_time": data.get("server_time"),
                    "simulated": False,
                })

    def record_metrics(self) -> None:
        with self.lock:
            tasks = list(self.tasks.values())
            parents = [x for x in tasks if x.get("type") == "parent"]
            children = [x for x in tasks if x.get("type") == "child"]
            hn, cq = self.nodes["海南"], self.nodes["重庆"]

            def util(node: dict[str, Any]) -> float:
                gpus = node.get("gpus") or []
                if not gpus:
                    return 0.0
                return round(sum(float(g.get("utilization_pct", 0)) for g in gpus) / len(gpus), 1)

            point = {
                "ts": now_iso(),
                "hainan_free_gb": float(hn.get("free_gb") or 0),
                "chongqing_free_gb": float(cq.get("free_gb") or 0),
                "hainan_util_pct": util(hn),
                "chongqing_util_pct": util(cq),
                "hainan_peak_util_pct": self._peak_util["海南"],
                "chongqing_peak_util_pct": self._peak_util["重庆"],
                "running": sum(1 for x in children if x.get("status") == "RUNNING"),
                "queued": sum(1 for x in children if x.get("status") == "QUEUED"),
                "succeeded_shards": sum(1 for x in children if x.get("status") == "SUCCEEDED"),
                "failed_shards": sum(1 for x in children if x.get("status") in (
                    "FAILED", "FAILED-LINK", "UNSCHEDULED", "FAILED-TIMEOUT", "CANCELLED",
                )),
                "parent_success_rate": round(
                    (sum(1 for x in parents if x.get("status") == "SUCCEEDED") / len(parents) * 100) if parents else 0.0,
                    1,
                ),
            }
            self._peak_util["海南"] = max(self._peak_util["海南"], float(point["hainan_util_pct"] or 0))
            self._peak_util["重庆"] = max(self._peak_util["重庆"], float(point["chongqing_util_pct"] or 0))
            point["hainan_peak_util_pct"] = self._peak_util["海南"]
            point["chongqing_peak_util_pct"] = self._peak_util["重庆"]
            self.metrics.append(point)
            self.metrics = self.metrics[-180:]

    def node_snapshot(self, node: dict[str, Any]) -> dict[str, Any]:
        gpus = []
        region_peak = 0.0
        for gpu in node.get("gpus") or []:
            gid = str(gpu.get("id") or "")
            cur = float(gpu.get("utilization_pct") or 0)
            peak = float(gpu.get("peak_utilization_pct") or self._gpu_peak_util.get(gid, 0) or 0)
            peak = max(peak, cur)
            region_peak = max(region_peak, peak)
            gpus.append({
                **gpu,
                "used_gb": round(float(gpu.get("total_gb", 0)) - float(gpu.get("free_gb", 0)), 2),
                "load_pct": gpu.get("load_pct", 0),
                "utilization_pct": cur,
                "peak_utilization_pct": round(peak, 1),
            })
        return {
            "region": node["region"], "model": node.get("model"), "rtt_ms": node.get("rtt_ms"),
            "cost": node.get("cost"), "healthy": node.get("healthy"), "link_up": node.get("link_up"),
            "reachable": node.get("reachable"), "free_gb": float(node.get("free_gb") or 0),
            "gpus": gpus, "agent_url": node.get("agent_url"), "last_error": node.get("last_error") or "",
            "simulated": bool(node.get("simulated")), "has_tee": bool(node.get("has_tee")),
            "green_factor": float(node.get("green_factor") or 0),
            "gpu_capacity": int(node.get("gpu_capacity") or len(gpus)),
            "gpu_free": int(node.get("gpu_free") or sum(1 for g in gpus if float(g.get("free_gb", 0)) >= 1)),
            "region_tag": node.get("region_tag") or "",
            "peak_utilization_pct": round(region_peak, 1),
        }

    def choose(self, memory_gb: float, mode: str, affinity: str | None = None) -> tuple[str | None, dict[str, Any]]:
        with self.lock:
            # 真实派发仅使用有 agent 的节点；调度算法在其上传打分
            dispatchable = {
                name: dict(node)
                for name, node in self.nodes.items()
                if name in self.agent_urls
            }
        region, decision = choose_with_paper(
            dispatchable,
            memory_gb=memory_gb,
            mode=mode or "动态权重多目标",
            affinity=affinity,
        )
        with self.lock:
            self.last_decision = dict(decision)
        return region, decision

    def run_paper_bench(self) -> dict[str, Any]:
        out_dir = EVIDENCE_ROOT / "experiment"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            out_dir = ROOT.parents[1] / "reports" / "data"
            out_dir.mkdir(parents=True, exist_ok=True)
        payload = run_paper_experiment(out_dir)
        # 精简返回，避免巨大 JSON
        slim = {
            "summaries": payload["summaries"],
            "ablation": payload["ablation"],
            "tasks": payload["tasks"],
            "paper_rows": [
                {
                    "task_id": r["task_id"],
                    "selected": r["selected"],
                    "status": r["status"],
                    "latency_ms": r.get("latency_ms"),
                    "cost": r.get("cost"),
                    "energy": r.get("energy"),
                    "s_t": r.get("s_t"),
                    "scores": r.get("scores"),
                    "reason": r.get("reason"),
                }
                for r in payload["paper_rows"]
            ],
            "weights": dict(DEFAULT_WEIGHTS),
            "nodes": payload["nodes"],
            "output_dir": str(out_dir),
        }
        with self.lock:
            self.paper_cache = slim
        self.log("SYSTEM", "BENCH_EXPERIMENT", "已完成 30 任务对比实验", success=slim["summaries"]["本文方法（动态权重多目标调度）"]["success_rate_pct"])
        return slim

    def agent_toggle(self, region: str, field: str) -> dict[str, Any]:
        node = self.nodes.get(region) or {}
        if node.get("simulated") or region not in self.agent_urls:
            with self.lock:
                self.nodes[region][field] = not bool(self.nodes[region].get(field))
            return {"ok": True, "simulated": True, field: self.nodes[region][field]}
        url = self.agent_urls[region]
        return http_json("POST", f"{url}/v1/admin/toggle", {"field": field}, timeout=5.0)

    def dispatch(self, region: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._agent_url(region)
        if url == "__unreachable__":
            return {"error": "hainan unreachable (vpn down)", "_unreachable": True}
        return http_json("POST", f"{url}/v1/tasks", payload, timeout=15.0)

    def poll_remote(self, region: str, task_id: str) -> dict[str, Any]:
        url = self._agent_url(region)
        if url == "__unreachable__":
            return {"error": "unreachable", "_unreachable": True}
        return http_json("GET", f"{url}/v1/tasks/{task_id}", timeout=5.0)

    def cancel_remote(self, region: str, task_id: str) -> dict[str, Any]:
        url = self._agent_url(region)
        if url == "__unreachable__":
            return {"error": "unreachable", "_unreachable": True}
        return http_json("POST", f"{url}/v1/tasks/{task_id}/cancel", {}, timeout=5.0)

    def cleanup_remote(self, region: str, task_id: str) -> dict[str, Any]:
        url = self._agent_url(region)
        if url == "__unreachable__":
            return {"error": "unreachable", "_unreachable": True}
        return http_json("POST", f"{url}/v1/cleanup", {"task_id": task_id}, timeout=5.0)

    def fetch_predictions(self, region: str, task_id: str) -> bytes | None:
        url = self._agent_url(region)
        if url == "__unreachable__":
            return None
        return http_fetch_bytes(f"{url}/v1/tasks/{task_id}/files/predictions.jsonl", timeout=60.0)

    def set_task(self, task_id: str, **updates: Any) -> None:
        with self.lock:
            self.tasks[task_id].update(updates)

    def start_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        aliases = {
            "cross_region": "dt01_cross_region",
            "vram16": "dt03_vram16",
            "otn_outage": "dt04_otn_outage",
        }
        scenario = aliases.get(str(payload.get("scenario") or "dt01_cross_region"), str(payload.get("scenario") or "dt01_cross_region"))
        if scenario not in SCENARIOS:
            return {"error": f"unknown scenario: {scenario}"}
        meta = SCENARIOS[scenario]
        defaults = meta["defaults"]

        if scenario == "tc04_hash_mismatch":
            bad_hash = "0000000000000000000000000000000000000000000000000000000000000000"
            check = verify_bundle_integrity(expect_tar_hash=bad_hash)
            self.log("SYSTEM", "HASH_MISMATCH", "故意数据集哈希不一致，拒绝创建任务", check=check)
            with self.lock:
                self.sequence += 1
                parent = str(payload.get("task_id") or f"POC-REAL-{self.sequence:04d}")
            task = {
                "task_id": parent, "parent_id": parent, "type": "parent", "status": "REJECTED",
                "scenario": scenario, "scenario_code": meta["code"], "scenario_title": meta["title"],
                "created_at": now_iso(), "finished_at": now_iso(), "progress": 100,
                "message": "数据集哈希校验失败，未创建可执行任务",
                "integrity": check, "audit": "HASH_MISMATCH recorded",
            }
            with self.lock:
                self.tasks[parent] = task
            self.evidence.write_case_evidence({**task, "_children": []}, self.events[:20], None)
            return dict(task)

        if scenario == "tc03_unauth_port":
            with self.lock:
                self.sequence += 1
                parent = str(payload.get("task_id") or f"POC-REAL-{self.sequence:04d}")
            probe = probe_closed_port()
            self.log(parent, "PROBE_BLOCKED", "非授权端口探针被拒绝", probe=probe)
            task = {
                "task_id": parent, "parent_id": parent, "type": "parent", "status": "REJECTED",
                "scenario": scenario, "scenario_code": meta["code"], "scenario_title": meta["title"],
                "created_at": now_iso(), "finished_at": now_iso(), "progress": 100,
                "message": "非授权端口/路径探针被阻断，未创建执行任务", "probe": probe,
            }
            with self.lock:
                self.tasks[parent] = task
            self.log(parent, "COMPLETE", "TC03 探针审计完成", status="REJECTED")
            self.evidence.write_case_evidence({**task, "_children": []}, self.events[:20], None)
            return dict(task)

        if scenario == "tc02_auth_fail":
            with self.lock:
                self.sequence += 1
                parent = str(payload.get("task_id") or f"POC-REAL-{self.sequence:04d}")
            bad = http_json("GET", f"{HAINAN_URL}/v1/resources", token="invalid-token-demo", timeout=3.0)
            cq_bad = http_json("GET", f"{CHONGQING_URL}/v1/resources", token="invalid-token-demo", timeout=3.0)
            self.log(parent, "AUTH_FAIL", "错误 Bearer 访问 agent 被拒绝", hainan=bad, chongqing=cq_bad)
            task = {
                "task_id": parent, "parent_id": parent, "type": "parent", "status": "REJECTED",
                "scenario": scenario, "scenario_code": meta["code"], "scenario_title": meta["title"],
                "created_at": now_iso(), "finished_at": now_iso(), "progress": 100,
                "message": "认证失败已记录审计，未创建可执行 GPU 任务",
                "auth_probe": {"hainan_status": bad.get("_http_status"), "chongqing_status": cq_bad.get("_http_status")},
            }
            with self.lock:
                self.tasks[parent] = task
            self.log(parent, "COMPLETE", "TC02 认证失败审计完成", status="REJECTED")
            self.evidence.write_case_evidence({**task, "_children": []}, self.events[:20], None)
            return dict(task)

        integrity: dict[str, Any] | None = None
        if scenario in ("tc01_ingress", "dt01_cross_region", "tc20_return", "tc29_audit", "normal") or scenario in RESNET_SCENARIOS:
            integrity = verify_bundle_integrity()
            if scenario == "tc01_ingress" and not integrity.get("ok"):
                self.log("SYSTEM", "INGRESS_REJECT", "TC01 完整性校验失败", integrity=integrity)
                return {"error": "input_manifest/SHA256SUMS 校验失败", "integrity": integrity}

        with self.lock:
            self.sequence += 1
            parent = str(payload.get("task_id") or f"POC-REAL-{self.sequence:04d}")
            if parent in self.tasks:
                return {"error": "task_id already exists", "task_id": parent}
            count = max(1, min(int(payload.get("shards", defaults["shards"])), 16))
            memory = float(payload.get("memory_gb", defaults["memory_gb"]))
            mode = str(payload.get("mode") or defaults.get("mode") or "动态权重多目标")
            duration = int(payload.get("duration_sec", defaults.get("duration_sec", 30)))

            if scenario == "dt05_isolation":
                created = []
                for tag in ("A", "B"):
                    pid = f"{parent}-{tag}"
                    created.append(self._create_parent_locked(pid, scenario, mode, memory, count, duration, meta, integrity))
                    threading.Thread(target=self._run_parent, args=(pid,), daemon=True).start()
                return {"task_id": parent, "parents": created, "scenario": scenario, "mode": MODE}

            task = self._create_parent_locked(parent, scenario, mode, memory, count, duration, meta, integrity)
            threading.Thread(target=self._run_parent, args=(parent,), daemon=True).start()
            return dict(task)

    def _create_parent_locked(
        self,
        parent: str,
        scenario: str,
        mode: str,
        memory: float,
        count: int,
        duration: int,
        meta: dict[str, Any],
        integrity: dict[str, Any] | None,
    ) -> dict[str, Any]:
        task = {
            "task_id": parent, "parent_id": parent, "type": "parent", "status": "SUBMITTED",
            "scenario": scenario, "scenario_code": meta["code"], "scenario_title": meta["title"],
            "mode": mode, "memory_gb": memory, "shards": count, "duration_sec": duration,
            "created_at": now_iso(), "progress": 0, "success_shards": 0, "failed_shards": 0,
            "regions": [], "result_sha256": "", "message": "已登记，等待真实 ResNet 调度",
            "cleanup_record": None, "integrity_check": integrity or {},
            "prediction_parts": [],
        }
        self.tasks[parent] = task
        for i in range(1, count + 1):
            child_id = f"{parent}-S{i:02d}"
            self.tasks[child_id] = {
                "task_id": child_id, "parent_id": parent, "type": "child", "shard": i, "shards": count,
                "status": "QUEUED", "scenario": scenario, "mode": mode, "memory_gb": memory,
                "duration_sec": duration, "progress": 0, "selected_region": None, "gpu_id": None,
                "reason": "等待调度", "created_at": now_iso(), "updated_at": now_iso(),
                "result_sha256": "", "retry_of": None, "message": "QUEUED",
                "accepted": [], "rejected": [], "workload": "resnet",
                "shard_manifest": f"manifest_part-{i:02d}.csv",
            }
        self.log(parent, "SUBMIT", f"[{meta['code']}] 真实 ResNet 父任务创建，{count}个分片", scenario=scenario)
        return dict(task)

    def _shard_payload(
        self,
        child_id: str,
        parent_id: str,
        scenario: str,
        memory: float,
        duration: int,
        shard: int,
        workload: str = "resnet",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": child_id,
            "parent_id": parent_id,
            "memory_gb": memory,
            "duration_sec": duration,
            "scenario": scenario,
            "seed": 202607,
            "workload": workload,
        }
        if workload == "resnet":
            payload["shard_manifest"] = f"manifest_part-{shard:02d}.csv"
        if scenario == "tc18_timeout":
            payload["force_fail"] = True
        if scenario == "tc27_bad_image":
            payload["bad_image"] = True
        return payload

    def _run_parent(self, parent_id: str) -> None:
        parent = self.tasks[parent_id]
        count = int(parent["shards"])
        mode = parent["mode"]
        scenario = parent["scenario"]
        memory = float(parent["memory_gb"])
        duration = int(parent.get("duration_sec") or 30)
        prediction_files: list[Path] = []
        with self.lock:
            self._peak_util = {"海南": 0.0, "重庆": 0.0}
            self._gpu_peak_util = {}
        self.set_task(parent_id, status="RUNNING", message="正在向真实节点分发任务分片")
        self.refresh_nodes()

        if scenario == "tc06_resource_discover":
            snap = [self.node_snapshot(n) for n in self.nodes.values()]
            self.log(parent_id, "RESOURCE_SNAPSHOT", "已采集两地真实 GPU 快照", snapshot=snap)
            digest = hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self.set_task(
                parent_id, status="SUCCEEDED", progress=100, success_shards=1, finished_at=now_iso(),
                result_sha256=digest, message="真实资源发现完成",
                regions=[r for r, n in self.nodes.items() if n.get("reachable")],
            )
            self._finalize_parent(parent_id, prediction_files)
            return

        if scenario == "tc22_node_offline":
            self.agent_toggle("重庆", "healthy")
            self.refresh_nodes()
            self.log(parent_id, "NODE_OFFLINE", "已将重庆 agent 标记 unhealthy")

        if scenario == "tc12_backpressure":
            self._fill_capacity(parent_id, duration=max(duration, 20))

        if scenario == "tc24_vpn_down":
            with self.lock:
                self.hainan_unreachable = True
            self.refresh_nodes()
            self.log(parent_id, "VPN_DOWN", "模拟新加坡—海南 VPN 中断，海南 agent 不可达")

        for i in range(1, count + 1):
            child_id = f"{parent_id}-S{i:02d}"
            affinity = None
            if scenario in ("dt01_cross_region", "dt04_otn_outage", "tc01_ingress", "dt05_isolation", "tc20_return", "demo_util") and i <= 2:
                affinity = "海南"
            elif scenario in ("dt01_cross_region", "dt04_otn_outage", "tc01_ingress", "dt05_isolation", "tc20_return", "demo_util") and i in (3, 4):
                affinity = "重庆"

            if scenario == "dt02_divert" and i == 2:
                self._reserve_hainan(parent_id)
                self.refresh_nodes()

            if scenario == "dt04_otn_outage" and i == 3:
                self.agent_toggle("重庆", "link_up")
                self.refresh_nodes()
                self.log(child_id, "LINK_DOWN", "已断开重庆 agent 链路(模拟 OTN 中断)")

            if scenario == "tc24_vpn_down" and i == 2:
                with self.lock:
                    self.hainan_unreachable = False
                self.refresh_nodes()
                self.log(parent_id, "VPN_RECOVERED", "海南 VPN 恢复，重新纳管")

            workload = "resnet"
            run_duration = duration
            if scenario == "tc17_cancel":
                workload = "gpu_load"
                run_duration = max(duration, 120)
            elif scenario == "demo_util":
                workload = "gpu_load"
                run_duration = max(duration, 25)

            self.set_task(child_id, status="SCHEDULING", updated_at=now_iso(), message="查询真实节点资源并做硬约束过滤")
            self.refresh_nodes()
            region, decision = self.choose(memory, mode, affinity)
            # 隧道偶发超时：短暂重试一次，避免亲和分片被瞬时抖动打成 UNSCHEDULED
            if region is None:
                rejected = decision.get("rejected") or []
                transient = any(
                    ("timed out" in str(r.get("reason") or "").lower())
                    or ("探测抖动" in str(r.get("reason") or ""))
                    or ("unreachable" in str(r.get("reason") or "").lower())
                    for r in rejected
                )
                if transient:
                    time.sleep(1.2)
                    self.refresh_nodes()
                    region, decision = self.choose(memory, mode, affinity)

            if region is None and scenario == "dt04_otn_outage" and affinity == "重庆":
                self.set_task(
                    child_id, status="FAILED-LINK", reason=decision.get("reason"),
                    rejected=decision.get("rejected", []), message="重庆路径不可用", updated_at=now_iso(),
                )
                self.log(child_id, "FAIL", "重庆分片因链路失败", rejected=decision.get("rejected", []))
                self.set_task(child_id, status="RETRYING", retry_of=child_id, message="关联重试至海南")
                region, decision = self.choose(memory, "海南优先", "海南")

            if region is None and scenario == "tc24_vpn_down" and affinity == "海南" and self.hainan_unreachable:
                self.set_task(
                    child_id, status="FAILED-LINK", reason="海南 VPN 不可达",
                    message="VPN 中断导致海南不可调度", updated_at=now_iso(),
                )
                self.log(child_id, "VPN_FAIL", "海南分片因 VPN 中断失败")
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue

            if region is None:
                status = "UNSCHEDULED"
                self.set_task(
                    child_id, status=status, reason=decision.get("reason"),
                    rejected=decision.get("rejected", []), message=decision.get("reason") or "无候选",
                    updated_at=now_iso(), progress=100,
                )
                self.log(child_id, "UNSCHEDULED", decision.get("reason") or "", rejected=decision.get("rejected", []))
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue

            payload = self._shard_payload(child_id, parent_id, scenario, memory, run_duration, i, workload=workload)
            self.set_task(
                child_id, status="DISPATCHING", selected_region=region, gpu_id=decision.get("gpu_id"),
                reason=decision.get("reason"), accepted=decision.get("accepted", []),
                rejected=decision.get("rejected", []), message=f"向{region} agent 下发 {workload}",
                updated_at=now_iso(), progress=10, workload=workload,
                selected_metrics=decision.get("selected_metrics") or {},
                score=decision.get("score"),
            )
            self.log(
                child_id, "DISPATCH", f"真实下发到{region} ({workload})",
                region=region, reason=decision.get("reason"), workload=workload,
            )

            remote = self.dispatch(region, payload)
            if remote.get("_unreachable") or (remote.get("error") and remote.get("status") is None and remote.get("task_id") is None):
                self.set_task(
                    child_id, status="FAILED", message=remote.get("error") or "agent unreachable",
                    reason="agent_error", progress=100, finished_at=now_iso(),
                )
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                self.log(child_id, "FAIL", remote.get("error") or "agent unreachable")
                continue

            if remote.get("status") in ("UNSCHEDULED", "FAILED", "FAILED-TIMEOUT"):
                self.set_task(
                    child_id, status=remote["status"],
                    message=remote.get("message") or remote.get("error") or remote["status"],
                    reason=remote.get("reason"), progress=100, finished_at=now_iso(),
                    selected_region=region, gpu_id=remote.get("gpu_id"),
                )
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue

            cancel_sent = False
            terminal = {"SUCCEEDED", "FAILED", "FAILED-TIMEOUT", "UNSCHEDULED", "CANCELLED"}
            for tick in range(240):
                time.sleep(0.7)
                if scenario == "tc17_cancel" and not cancel_sent and tick >= 3:
                    cancel_sent = True
                    cr = self.cancel_remote(region, child_id)
                    self.log(child_id, "CANCEL", "已向 agent 发送取消请求", result=cr)
                st = self.poll_remote(region, child_id)
                if st.get("_unreachable"):
                    continue
                status = st.get("status") or "RUNNING"
                self.set_task(
                    child_id,
                    status=status if status != "STARTING" else "RUNNING",
                    progress=st.get("progress", 50),
                    gpu_id=st.get("gpu_id") or decision.get("gpu_id"),
                    selected_region=region,
                    result_sha256=st.get("result_sha256") or "",
                    message=st.get("message") or status,
                    updated_at=now_iso(),
                    reason=decision.get("reason"),
                )
                if status in terminal:
                    break

            final = self.tasks[child_id]
            if final.get("status") == "SUCCEEDED" and workload == "resnet":
                raw = self.fetch_predictions(region, child_id)
                if raw:
                    path = self.evidence.save_predictions(parent_id, child_id, raw)
                    prediction_files.append(path)
                    self.log(child_id, "EVIDENCE", f"已保存 predictions 至 {path}", bytes=len(raw))
                with self.lock:
                    self.tasks[parent_id]["success_shards"] += 1
                    if region not in self.tasks[parent_id]["regions"]:
                        self.tasks[parent_id]["regions"].append(region)
                    self.tasks[parent_id]["progress"] = round(i / count * 100, 1)
                self.log(child_id, "SUCCEEDED", "真实分片完成", region=region, result_sha256=final.get("result_sha256"))
            elif final.get("status") == "SUCCEEDED":
                with self.lock:
                    self.tasks[parent_id]["success_shards"] += 1
                    if region not in self.tasks[parent_id]["regions"]:
                        self.tasks[parent_id]["regions"].append(region)
                    self.tasks[parent_id]["progress"] = round(i / count * 100, 1)
                self.log(child_id, "SUCCEEDED", "分片完成", region=region)
            else:
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                self.log(child_id, "FAIL", final.get("message") or final.get("status") or "failed", region=region)

        if scenario == "dt04_otn_outage" and not self.nodes["重庆"].get("link_up"):
            self.agent_toggle("重庆", "link_up")
            self.refresh_nodes()
            self.log(parent_id, "LINK_RECOVERED", "重庆链路已恢复")

        if scenario == "tc22_node_offline" and not self.nodes["重庆"].get("healthy"):
            self.agent_toggle("重庆", "healthy")
            self.refresh_nodes()
            self.log(parent_id, "NODE_RECOVERED", "重庆节点已恢复纳管")

        if scenario == "tc24_vpn_down" and self.hainan_unreachable:
            with self.lock:
                self.hainan_unreachable = False
            self.refresh_nodes()
            self.log(parent_id, "VPN_RECOVERED", "海南 VPN 标志已恢复")

        self._finalize_parent(parent_id, prediction_files)

    def _finalize_parent(self, parent_id: str, prediction_files: list[Path]) -> None:
        merge_summary: dict[str, Any] | None = None
        with self.lock:
            parent = self.tasks[parent_id]
            scenario = parent["scenario"]
            count = int(parent["shards"])
            success = int(parent["success_shards"])
            failed = int(parent["failed_shards"])
            if prediction_files and scenario in RESNET_SCENARIOS:
                expect = 512 * len(prediction_files) if count <= 8 else 4096
                if scenario == "dt01_cross_region":
                    expect = 4096
                merge_summary = self.evidence.merge_predictions(parent_id, prediction_files, expect_samples=expect)
                if merge_summary.get("ok"):
                    out = Path(str(merge_summary.get("output", "")))
                    if out.is_file():
                        parent["result_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
                    parent["merge_summary"] = merge_summary
                parent["prediction_parts"] = [str(p) for p in prediction_files]

            if not parent.get("result_sha256") and success:
                digests = [
                    self.tasks.get(f"{parent_id}-S{i:02d}", {}).get("result_sha256", "")
                    for i in range(1, count + 1)
                ]
                parent["result_sha256"] = hashlib.sha256("|".join(digests).encode()).hexdigest()

            if scenario == "tc17_cancel":
                parent["status"] = "CANCELLED" if failed else "SUCCEEDED"
            elif scenario in ("tc18_timeout", "tc27_bad_image"):
                parent["status"] = "FAILED" if failed else "SUCCEEDED"
            elif success == count:
                parent["status"] = "SUCCEEDED"
            elif success:
                parent["status"] = "PARTIAL"
            else:
                parent["status"] = "FAILED"

            parent["progress"] = 100 if success == count else round(success / max(1, count) * 100, 1)
            parent["finished_at"] = now_iso()
            parent["message"] = f"{success}/{count}个真实分片完成"

            children = [dict(self.tasks[f"{parent_id}-S{i:02d}"]) for i in range(1, count + 1) if f"{parent_id}-S{i:02d}" in self.tasks]
            parent["_children"] = children

            if scenario in ("dt05_isolation", "tc28_cleanup") and success:
                cleaned = []
                for i in range(1, count + 1):
                    cid = f"{parent_id}-S{i:02d}"
                    region = self.tasks[cid].get("selected_region")
                    if region:
                        cleaned.append(self.cleanup_remote(region, cid))
                parent["cleanup_record"] = {
                    "ts": now_iso(), "results": cleaned,
                    "objects": ["predictions.jsonl", "metrics.json", "stdout.log", "work_dir"],
                }
                self.log(parent_id, "CLEANUP", "已触发节点清理", cleanup=parent["cleanup_record"])

            if scenario in ("dt05_isolation", "tc20_return") and parent["status"] == "SUCCEEDED":
                self.log(parent_id, "RETURN", "结果已汇聚至新加坡控制面 evidence", result_sha256=parent.get("result_sha256"))

        self.log(parent_id, "COMPLETE", self.tasks[parent_id]["message"], status=self.tasks[parent_id]["status"])
        outcome = self._build_outcome(parent_id)
        with self.lock:
            self.tasks[parent_id]["outcome"] = outcome
            self.last_outcome = outcome
            self.outcomes.insert(0, outcome)
            self.outcomes = self.outcomes[:20]
        self.log(
            parent_id, "OUTCOME",
            outcome.get("headline") or "已生成收益摘要",
            success_rate_pct=outcome.get("success_rate_pct"),
            cost_saving_pct=outcome.get("cost_saving_pct"),
            peak_util=outcome.get("peak_util"),
        )
        self.evidence.write_case_evidence(self.tasks[parent_id], self.events[:30], merge_summary)

    def _build_outcome(self, parent_id: str) -> dict[str, Any]:
        """测试完成后的收益可视化数据：落点、成本/时延相对基线、峰值利用率。"""
        with self.lock:
            parent = dict(self.tasks[parent_id])
            count = int(parent.get("shards") or 0)
            children = [
                dict(self.tasks[f"{parent_id}-S{i:02d}"])
                for i in range(1, count + 1)
                if f"{parent_id}-S{i:02d}" in self.tasks
            ]
            hn_cost = float(self.nodes["海南"].get("cost") or 2.5)
            cq_cost = float(self.nodes["重庆"].get("cost") or 2.0)
            hn_rtt = float(self.nodes["海南"].get("rtt_ms") or 15)
            cq_rtt = float(self.nodes["重庆"].get("rtt_ms") or 20)
            peak = dict(self._peak_util)
            mode = str(parent.get("mode") or "")

        ok = [c for c in children if c.get("status") == "SUCCEEDED"]
        fail = [c for c in children if c.get("status") not in ("SUCCEEDED",)]
        dist: dict[str, int] = {}
        for c in ok:
            r = str(c.get("selected_region") or "未知")
            dist[r] = dist.get(r, 0) + 1

        def shard_cost(c: dict[str, Any], region: str | None = None) -> float:
            # 统一用节点单价估算，保证「实际 vs 单边」可比
            reg = region or c.get("selected_region") or "海南"
            unit = hn_cost if reg == "海南" else cq_cost
            mem = float(c.get("memory_gb") or parent.get("memory_gb") or 8)
            return round(unit * (mem / 8.0) * 0.35, 4)

        def shard_lat(c: dict[str, Any], region: str | None = None) -> float:
            reg = region or c.get("selected_region") or "海南"
            m = c.get("selected_metrics") or {}
            if region is None and m.get("latency_ms") is not None:
                return float(m["latency_ms"])
            return hn_rtt + 8.0 if reg == "海南" else cq_rtt + 8.0

        actual_cost = round(sum(shard_cost(c) for c in ok), 4) if ok else 0.0
        actual_lat = round(sum(shard_lat(c) for c in ok) / len(ok), 2) if ok else 0.0
        # 反事实：全部落海南 / 全部落重庆（同成功分片数）
        all_hn_cost = round(sum(shard_cost(c, "海南") for c in ok), 4) if ok else 0.0
        all_cq_cost = round(sum(shard_cost(c, "重庆") for c in ok), 4) if ok else 0.0
        all_hn_lat = round(sum(shard_lat(c, "海南") for c in ok) / len(ok), 2) if ok else 0.0
        all_cq_lat = round(sum(shard_lat(c, "重庆") for c in ok) / len(ok), 2) if ok else 0.0
        # 收益相对「更差单边」：成本相对更贵侧，时延相对更慢侧
        baseline_cost = max(all_hn_cost, all_cq_cost, actual_cost)
        baseline_lat = max(all_hn_lat, all_cq_lat, actual_lat)
        cost_saving = round(max(0.0, baseline_cost - actual_cost), 4)
        cost_saving_pct = round(100.0 * cost_saving / baseline_cost, 1) if baseline_cost > 0 else 0.0
        lat_improve = round(max(0.0, baseline_lat - actual_lat), 2)
        lat_improve_pct = round(100.0 * lat_improve / baseline_lat, 1) if baseline_lat > 0 else 0.0
        success_rate = round(100.0 * len(ok) / max(1, count), 1)
        cross = len(dist) >= 2
        peak_max = round(max(peak.get("海南", 0), peak.get("重庆", 0)), 1)

        bullets = [
            f"完成 {len(ok)}/{count} 分片，成功率 {success_rate}%",
            f"落点分布：" + ("、".join(f"{k} {v}" for k, v in dist.items()) if dist else "无成功分片"),
            f"相对单边基线，估算成本节省 {cost_saving:.2f}（{cost_saving_pct}%），时延改善 {lat_improve:.1f} ms（{lat_improve_pct}%）",
            f"观测峰值 GPU 利用率 海南 {peak.get('海南', 0):.1f}% / 重庆 {peak.get('重庆', 0):.1f}%",
        ]
        if cross:
            bullets.append("实现海南↔重庆跨域协同，避免单点挤占")
        if peak_max < 15 and any(c.get("workload") == "resnet" or parent.get("scenario", "").startswith("dt") for c in children):
            bullets.append("说明：ResNet 分片约 1–2 秒结束，瞬时利用率易被 1s 采样漏采；峰值与占位类场景更能体现负载")

        headline = (
            f"{parent.get('scenario_code') or parent.get('scenario')} · {mode}："
            f"成功率 {success_rate}% · 成本↓{cost_saving_pct}% · 峰值利用率 {peak_max}%"
        )
        return {
            "parent_id": parent_id,
            "scenario": parent.get("scenario"),
            "scenario_code": parent.get("scenario_code"),
            "mode": mode,
            "status": parent.get("status"),
            "finished_at": parent.get("finished_at") or now_iso(),
            "headline": headline,
            "success_shards": len(ok),
            "failed_shards": len(fail),
            "total_shards": count,
            "success_rate_pct": success_rate,
            "distribution": dist,
            "actual_cost": actual_cost,
            "baseline_cost": baseline_cost,
            "cost_saving": cost_saving,
            "cost_saving_pct": cost_saving_pct,
            "actual_latency_ms": actual_lat,
            "baseline_latency_ms": baseline_lat,
            "latency_improve_ms": lat_improve,
            "latency_improve_pct": lat_improve_pct,
            "peak_util": {"海南": round(peak.get("海南", 0), 1), "重庆": round(peak.get("重庆", 0), 1)},
            "cross_region": cross,
            "bullets": bullets,
            "bars": [
                {"label": "实际成本", "value": actual_cost, "unit": "元", "color": "#36d399"},
                {"label": "更贵单边", "value": baseline_cost, "unit": "元", "color": "#ffb454"},
                {"label": "实际时延", "value": actual_lat, "unit": "ms", "color": "#55a6ff"},
                {"label": "更慢单边", "value": baseline_lat, "unit": "ms", "color": "#94a3b8"},
            ],
            "counterfactual": {
                "all_hainan_cost": all_hn_cost,
                "all_chongqing_cost": all_cq_cost,
                "all_hainan_latency_ms": all_hn_lat,
                "all_chongqing_latency_ms": all_cq_lat,
            },
            "result_sha256": parent.get("result_sha256") or "",
        }

    def _reserve_hainan(self, parent_id: str) -> None:
        node = self.nodes["海南"]
        gpus = node.get("gpus") or []
        self.log(parent_id, "LOAD_INJECT", "向海南 GPU 下发 gpu_load 占位，制造显存压力", gpus=len(gpus))
        for g in gpus:
            free = float(g.get("free_gb") or 0)
            target = 20.0 if free >= 20.0 else max(4.0, free - 1.5)
            if target < 4.0 or free < 4.0:
                continue
            tid = f"{parent_id}-HOLD-HN{g['index']}"
            self.dispatch("海南", {
                "task_id": tid, "parent_id": parent_id, "memory_gb": target,
                "duration_sec": 60, "scenario": "hold", "workload": "gpu_load",
            })
        time.sleep(2.0)
        self.refresh_nodes()

    def _fill_capacity(self, parent_id: str, duration: int = 20) -> None:
        self.refresh_nodes()
        for region, node in self.nodes.items():
            for g in node.get("gpus") or []:
                free = float(g.get("free_gb") or 0)
                if free < 2:
                    continue
                tid = f"{parent_id}-FILL-{region}-{g['index']}"
                self.dispatch(region, {
                    "task_id": tid, "parent_id": parent_id,
                    "memory_gb": max(2.0, free - 0.8),
                    "duration_sec": duration, "scenario": "fill", "workload": "gpu_load",
                })
        time.sleep(2.0)
        self.refresh_nodes()
        self.log(parent_id, "BACKPRESSURE", "已尝试占满两地 GPU")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tasks = [dict(v) for v in self.tasks.values()]
            tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            parents = [x for x in tasks if x.get("type") == "parent"]
            counts: dict[str, int] = {}
            for key in (
                "SUCCEEDED", "RUNNING", "QUEUED", "FAILED-LINK", "UNSCHEDULED",
                "FAILED-TIMEOUT", "FAILED", "PARTIAL", "DISPATCHING", "SCHEDULING",
                "CANCELLED", "REJECTED", "RETRYING",
            ):
                counts[key] = sum(1 for x in tasks if x.get("status") == key)
            return {
                "mode": MODE,
                "fabric": FABRIC,
                "algorithm": {
                    "name": "自适应动态权重多目标调度",
                    "weights": dict(DEFAULT_WEIGHTS),
                    "modes": ["动态权重多目标", "海南优先", "最小延迟", "最小成本", "静态本地", "先到先服务"],
                },
                "last_decision": self.last_decision,
                "last_outcome": self.last_outcome,
                "outcomes": list(self.outcomes[:8]),
                "peak_util": dict(self._peak_util),
                "paper_summary": None if not self.paper_cache else {
                    "summaries": self.paper_cache.get("summaries"),
                    "ablation": self.paper_cache.get("ablation"),
                    "weights": self.paper_cache.get("weights"),
                },
                "reality": {
                    "scheduler": "adaptive-dynamic-weight",
                    "note": (
                        "调度核心：硬约束（GPU/时延/预算/TEE）过滤 + 最小-最大规范化 + S(t) 自适应加权。"
                        f"资源织物模式={FABRIC}；真实 GPU 执行通过海南/重庆 node agent 下发 ResNet 分片。"
                    ),
                    "agents": {"海南": HAINAN_URL, "重庆": CHONGQING_URL},
                    "bundle": str(BUNDLE),
                    "evidence_root": str(EVIDENCE_ROOT),
                    "ports": {
                        "ui": "新加坡 8080",
                        "agent": "计算节点 8000（海南经反向隧道映射到新加坡 127.0.0.1:18000）",
                        "probe": "80/8080 仍可用于连通性探针",
                    },
                },
                "server_time": now_iso(),
                "scenarios": list(SCENARIOS.values()),
                "nodes": [self.node_snapshot(n) for n in self.nodes.values()],
                "tasks": tasks[:120],
                "parents": parents[:40],
                "events": list(self.events[:100]),
                "metrics": list(self.metrics[-120:]),
                "stats": {
                    "total": len(parents),
                    "succeeded": sum(1 for x in parents if x["status"] == "SUCCEEDED"),
                    **counts,
                },
            }


STATE = SchedulerState()


class Handler(BaseHTTPRequestHandler):
    server_version = "L1SchedulerUI/3.0-adaptive"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if path == "/api/health":
                self.send_json({"ok": True, "mode": MODE, "auth_required": bool(TOKEN)})
                return
            if path == "/api/scenarios":
                self.send_json({"scenarios": list(SCENARIOS.values())})
                return
            if not self.authorized():
                self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            if path in ("/api/status", "/api/tasks", "/api/events", "/api/metrics"):
                self.send_json(STATE.snapshot())
                return
            if path == "/api/paper/experiment":
                if STATE.paper_cache:
                    self.send_json(STATE.paper_cache)
                else:
                    self.send_json(STATE.run_paper_bench())
                return
            if path == "/api/paper/nodes":
                self.send_json({"nodes": paper_fabric_nodes(), "weights": dict(DEFAULT_WEIGHTS)})
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self.serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.authorized():
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            payload = self.read_json()
            if path == "/api/tasks":
                result = STATE.start_task(payload)
                # TC02/TC04 等故意失败场景会带 error 字段，但已写入审计，应视为业务成功响应
                if result.get("status") in ("REJECTED", "FAILED") or result.get("audit"):
                    status = HTTPStatus.OK
                elif "error" in result:
                    status = HTTPStatus.CONFLICT if "already exists" in result.get("error", "") else HTTPStatus.BAD_REQUEST
                else:
                    status = HTTPStatus.ACCEPTED
                self.send_json(result, status)
                return
            if path == "/api/paper/experiment":
                self.send_json(STATE.run_paper_bench())
                return
            if path == "/api/reset":
                STATE.reset()
                self.send_json({"ok": True})
                return
            if path == "/api/nodes/toggle":
                region = str(payload.get("region"))
                field = str(payload.get("field"))
                if region not in STATE.nodes or field not in ("healthy", "link_up"):
                    self.send_json({"error": "invalid region or field"}, HTTPStatus.BAD_REQUEST)
                    return
                result = STATE.agent_toggle(region, field)
                STATE.refresh_nodes()
                STATE.log("SYSTEM", "NODE_TOGGLE", f"{region} {field} via agent", result=result)
                self.send_json({"ok": True, "region": region, "field": field, "agent_result": result})
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        else:
            content_type = "application/javascript; charset=utf-8"
        raw = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("SCHEDULER_UI_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SCHEDULER_UI_PORT", "8080")))
    args = parser.parse_args()
    try:
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        CASE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"warning: cannot create evidence dir {EVIDENCE_ROOT}: {exc}", flush=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"L1 REAL ResNet scheduler UI on http://{args.host}:{args.port} "
        f"hn={HAINAN_URL} cq={CHONGQING_URL} bundle={BUNDLE}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
