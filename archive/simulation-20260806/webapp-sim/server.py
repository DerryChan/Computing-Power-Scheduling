#!/usr/bin/env python3
"""L1 算力调度实时可视化服务。

默认是确定性仿真模式：不访问真实GPU、调度平台或跨区域网络，适合部署在
新加坡服务器上做实时演示和联调前的规则验证。服务只依赖Python标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
TOKEN = os.environ.get("SCHEDULER_UI_TOKEN", "").strip()
SIMULATION_MODE = os.environ.get("SCHEDULER_MODE", "simulation")

SCENARIOS: dict[str, dict[str, Any]] = {
    "dt01_cross_region": {
        "id": "dt01_cross_region",
        "code": "DT01",
        "title": "两地共同执行的分片批量推理",
        "level": "A",
        "summary": "一个父任务拆成 8 个分片，海南与重庆同时参与，海南最终汇聚结果。",
        "detail": (
            "对应报告 DT01 / TC13 / TC14 / TC15。\n"
            "操作含义：模拟新加坡上传后，海南登记父任务并生成 S01–S08；"
            "S01/S02 亲和海南，S03/S04 亲和重庆，其余按策略自动调度；"
            "每个分片独立占用一张 GPU，完成后生成结果哈希，父任务按 sample 口径汇聚。\n"
            "观察重点：两地是否都出现执行记录、父任务进度是否到 100%、结果 SHA-256 是否生成。"
        ),
        "defaults": {"shards": 8, "memory_gb": 8, "mode": "海南优先"},
    },
    "dt02_divert": {
        "id": "dt02_divert",
        "code": "DT02",
        "title": "海南优先与重庆自动分流",
        "level": "A",
        "summary": "先证明空闲时选海南；再预占海南显存，迫使同等任务分流到重庆。",
        "detail": (
            "对应报告 DT02 / TC08 / TC09。\n"
            "操作含义：系统先提交 1 个 8GB 标准子任务（应落海南）；"
            "随后在海南两张 4090 上各预占约 20GB；再提交相同任务，因单卡可用显存不足而自动选择重庆。\n"
            "观察重点：第一次选择海南及原因；第二次候选集中海南被“单卡可用显存不足”拒绝，最终选重庆。"
        ),
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "海南优先"},
    },
    "dt03_vram16": {
        "id": "dt03_vram16",
        "code": "DT03",
        "title": "单卡 16GB 显存硬约束",
        "level": "A",
        "summary": "16GB 任务在候选阶段排除重庆 12GB 单卡，只允许海南 4090。",
        "detail": (
            "对应报告 DT03 / TC10。\n"
            "操作含义：资源声明 gpu_memory=16GB。调度在候选节点阶段按“单卡可用显存”过滤，"
            "重庆 4070 的 12GB 不得被当成 48GB 统一显存池；不得先派重庆再 OOM。\n"
            "观察重点：重庆出现在 rejected 列表；任务只调度到海南。"
        ),
        "defaults": {"shards": 1, "memory_gb": 16, "mode": "海南优先"},
    },
    "dt04_otn_outage": {
        "id": "dt04_otn_outage",
        "code": "DT04",
        "title": "重庆链路中断、停派与分片重试",
        "level": "A",
        "summary": "重庆分片执行中模拟 OTN 中断：停派、失败分片关联重试至海南，再恢复链路。",
        "detail": (
            "对应报告 DT04 / TC23 / TC25。\n"
            "操作含义：父任务按两地亲和拆分；当重庆亲和分片到达时，模拟海南—重庆 OTN 中断，"
            "重庆不可接收新任务；失败分片以原子任务关联关系重试到海南；最后恢复链路。\n"
            "观察重点：LINK_DOWN 事件、FAILED-LINK→RETRYING、最终父任务仍可成功汇聚。"
        ),
        "defaults": {"shards": 8, "memory_gb": 8, "mode": "海南优先"},
    },
    "dt05_isolation": {
        "id": "dt05_isolation",
        "code": "DT05",
        "title": "结果汇聚、跨境回传与数据清理",
        "level": "A",
        "summary": "并行跑两个父任务 A/B，验证结果键隔离、哈希分离与清理留痕。",
        "detail": (
            "对应报告 DT05 / TC20 / TC21 / TC28。\n"
            "操作含义：同时创建父任务 A 与 B，各自 4 个分片、独立目录/结果键；"
            "汇聚后分别生成 result SHA-256；确认后清理输入、中间文件、临时容器和非必要缓存，并写清理记录。\n"
            "观察重点：A/B 结果哈希不同且不交叉；事件流出现 CLEANUP；无结果错配。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先"},
    },
    "tc06_resource_discover": {
        "id": "tc06_resource_discover",
        "code": "TC06",
        "title": "资源发现准确性快照",
        "level": "A",
        "summary": "刷新并展示海南/重庆 GPU 型号、数量、单卡显存与健康字段。",
        "detail": (
            "对应报告 TC06 / TC07。\n"
            "操作含义：不真正下发推理，而是采集仿真节点快照，核对字段完整率；"
            "随后制造一次负载变化，验证状态刷新带时间戳。\n"
            "观察重点：资源卡片字段完整；事件流出现 RESOURCE_SNAPSHOT / STATE_REFRESH。"
        ),
        "defaults": {"shards": 1, "memory_gb": 1, "mode": "海南优先"},
    },
    "tc12_backpressure": {
        "id": "tc12_backpressure",
        "code": "TC12",
        "title": "资源耗尽与背压排队",
        "level": "B",
        "summary": "占满可用显存后再提交任务，任务应进入排队/限流，而不是调度崩溃。",
        "detail": (
            "对应报告 TC12。\n"
            "操作含义：先占满海南与重庆所有单卡可用显存，再提交新任务；"
            "系统应返回排队或无候选，状态明确，服务保持可用。\n"
            "观察重点：出现 QUEUED/UNSCHEDULED；服务仍可查询状态；释放后可继续调度。"
        ),
        "defaults": {"shards": 2, "memory_gb": 8, "mode": "海南优先"},
    },
    "tc18_timeout": {
        "id": "tc18_timeout",
        "code": "TC18",
        "title": "超时及执行失败分类",
        "level": "A",
        "summary": "构造超时失败，任务转为 FAILED 并保留错误分类与原因。",
        "detail": (
            "对应报告 TC18。\n"
            "操作含义：分片启动后模拟超过冻结超时，进入 FAILED-TIMEOUT；"
            "父任务记录失败分片数，错误原因可查询。\n"
            "观察重点：状态不是“假成功”；事件含失败分类。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先"},
    },
    "tc22_node_offline": {
        "id": "tc22_node_offline",
        "code": "TC22",
        "title": "计算节点离线停派",
        "level": "A",
        "summary": "将重庆节点置为不健康，验证停止向其派发新任务。",
        "detail": (
            "对应报告 TC22 / TC26。\n"
            "操作含义：先标记重庆 unhealthy，再提交可调度到两地的任务；"
            "重庆不得进入候选接受集；恢复健康后可重新纳管。\n"
            "观察重点：rejected 原因为节点不健康；恢复后候选集重新包含重庆。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先"},
    },
    "tc27_bad_image": {
        "id": "tc27_bad_image",
        "code": "TC27",
        "title": "镜像摘要不一致拒绝调度",
        "level": "A",
        "summary": "提交错误镜像摘要，候选阶段拒绝，不残留失控容器。",
        "detail": (
            "对应报告 TC27 / TC11。\n"
            "操作含义：镜像 digest 与冻结值不一致时，在调度前失败为 UNSCHEDULED，"
            "保留失败原因，不进入 RUNNING。\n"
            "观察重点：无 GPU 占用；事件明确写明镜像校验失败。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先"},
    },
    "tc01_ingress": {
        "id": "tc01_ingress",
        "code": "TC01",
        "title": "正常跨境接入与完整性校验",
        "level": "A",
        "summary": "模拟新加坡有效提交：认证通过、登记、SHA-256 校验后进入调度。",
        "detail": (
            "对应报告 TC01 / TC04 / TC29。\n"
            "操作含义：输入包与冻结哈希一致则登记成功；随后进入分片调度；"
            "全过程写入审计事件（SUBMIT / DISPATCH / COMPLETE）。\n"
            "观察重点：审计字段含任务 ID、阶段、节点、时间戳。"
        ),
        "defaults": {"shards": 4, "memory_gb": 8, "mode": "海南优先"},
    },
    "normal": {
        "id": "normal",
        "code": "CUSTOM",
        "title": "自定义普通调度",
        "level": "—",
        "summary": "不附加演示剧本，仅按所选策略对满足硬约束的节点实时调度。",
        "detail": (
            "自由测试入口。\n"
            "可自行改分片数、显存和策略，并配合右侧“节点离线/断开链路”按钮观察候选集变化。"
        ),
        "defaults": {"shards": 1, "memory_gb": 8, "mode": "海南优先"},
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_gpu(region: str, index: int, model: str, memory_gb: float) -> dict[str, Any]:
    return {
        "id": f"{region}-GPU{index}",
        "model": model,
        "total_gb": memory_gb,
        "free_gb": memory_gb,
        "reserved_by": [],
        "temperature_c": 34,
        "utilization_pct": 0,
    }


class SchedulerState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.metrics: list[dict[str, Any]] = []
        self.sequence = 0
        self.nodes: dict[str, dict[str, Any]] = {}
        self._metrics_stop = False
        self.reset()
        self._metrics_thread = threading.Thread(target=self._metrics_loop, daemon=True)
        self._metrics_thread.start()

    def reset(self) -> None:
        with self.lock:
            self.nodes = {
                "海南": {
                    "region": "海南",
                    "model": "RTX 4090",
                    "rtt_ms": 25,
                    "cost": 2.50,
                    "green_factor": 0.80,
                    "healthy": True,
                    "link_up": True,
                    "gpus": [make_gpu("海南", 1, "RTX 4090", 24), make_gpu("海南", 2, "RTX 4090", 24)],
                },
                "重庆": {
                    "region": "重庆",
                    "model": "RTX 4070",
                    "rtt_ms": 45,
                    "cost": 2.00,
                    "green_factor": 0.60,
                    "healthy": True,
                    "link_up": True,
                    "gpus": [make_gpu("重庆", i, "RTX 4070", 12) for i in range(1, 5)],
                },
            }
            self.tasks = {}
            self.events = []
            self.metrics = []
            self.sequence = 0
            self.log("SYSTEM", "RESET", "仿真调度器已重置")

    def log(self, task_id: str, event: str, message: str, **extra: Any) -> None:
        item = {"ts": now_iso(), "task_id": task_id, "event": event, "message": message, **extra}
        with self.lock:
            self.events.insert(0, item)
            self.events = self.events[:300]

    def free_gb(self, node: dict[str, Any]) -> float:
        return round(sum(float(g["free_gb"]) for g in node["gpus"]), 2)

    def node_snapshot(self, node: dict[str, Any]) -> dict[str, Any]:
        gpus = []
        for gpu in node["gpus"]:
            total = float(gpu["total_gb"])
            free = float(gpu["free_gb"])
            gpus.append({**gpu, "free_gb": round(free, 2), "used_gb": round(total - free, 2),
                         "load_pct": round((total - free) / total * 100, 1)})
        return {
            "region": node["region"], "model": node["model"], "rtt_ms": node["rtt_ms"],
            "cost": node["cost"], "healthy": node["healthy"], "link_up": node["link_up"],
            "free_gb": self.free_gb(node), "gpus": gpus,
        }

    def _metrics_loop(self) -> None:
        while not self._metrics_stop:
            self.record_metrics()
            time.sleep(1.0)

    def record_metrics(self) -> None:
        with self.lock:
            tasks = list(self.tasks.values())
            parents = [x for x in tasks if x.get("type") == "parent"]
            children = [x for x in tasks if x.get("type") == "child"]
            hn = self.nodes["海南"]
            cq = self.nodes["重庆"]
            point = {
                "ts": now_iso(),
                "hainan_free_gb": self.free_gb(hn),
                "chongqing_free_gb": self.free_gb(cq),
                "hainan_util_pct": round(sum(g["utilization_pct"] for g in hn["gpus"]) / max(1, len(hn["gpus"])), 1),
                "chongqing_util_pct": round(sum(g["utilization_pct"] for g in cq["gpus"]) / max(1, len(cq["gpus"])), 1),
                "running": sum(1 for x in children if x.get("status") == "RUNNING"),
                "queued": sum(1 for x in children if x.get("status") == "QUEUED"),
                "succeeded_shards": sum(1 for x in children if x.get("status") == "SUCCEEDED"),
                "failed_shards": sum(1 for x in children if x.get("status") in ("FAILED-LINK", "UNSCHEDULED", "FAILED-TIMEOUT", "FAILED")),
                "parent_success_rate": round(
                    (sum(1 for x in parents if x.get("status") == "SUCCEEDED") / len(parents) * 100) if parents else 0.0,
                    1,
                ),
            }
            self.metrics.append(point)
            self.metrics = self.metrics[-180:]

    def choose(self, memory_gb: float, mode: str, affinity: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        with self.lock:
            accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
            rejected: list[dict[str, str]] = []
            for region, node in self.nodes.items():
                if affinity and region != affinity:
                    rejected.append({"region": region, "reason": "亲和性不匹配"})
                    continue
                if not node["healthy"]:
                    rejected.append({"region": region, "reason": "节点不健康"})
                    continue
                if region == "重庆" and not node["link_up"]:
                    rejected.append({"region": region, "reason": "海南—重庆链路不可用"})
                    continue
                gpu = next((g for g in node["gpus"] if g["free_gb"] >= memory_gb), None)
                if gpu is None:
                    rejected.append({"region": region, "reason": "单卡可用显存不足"})
                    continue
                accepted.append((node, gpu))
            if not accepted:
                return None, {"accepted": [], "rejected": rejected, "reason": "无满足硬约束的候选节点"}
            if mode == "海南优先" and any(n["region"] == "海南" for n, _ in accepted):
                node, gpu = next(item for item in accepted if item[0]["region"] == "海南")
                reason = "满足硬约束，命中海南优先规则"
            elif mode == "最小成本":
                node, gpu = min(accepted, key=lambda item: item[0]["cost"])
                reason = "候选节点中单位GPU成本最低"
            elif mode == "最小延迟":
                node, gpu = min(accepted, key=lambda item: item[0]["rtt_ms"])
                reason = "候选节点中预计往返时延最低"
            else:
                def score(item: tuple[dict[str, Any], dict[str, Any]]) -> float:
                    n = item[0]
                    return 0.50 * n["rtt_ms"] / 100 + 0.25 * n["cost"] / 3 + 0.15 * (1 - n["green_factor"]) + 0.10 * (1 - item[1]["free_gb"] / item[1]["total_gb"])
                node, gpu = min(accepted, key=score)
                reason = "延迟、成本、能耗和负载归一化加权得分最低"
            gpu["free_gb"] = round(gpu["free_gb"] - memory_gb, 2)
            gpu["reserved_by"].append("pending")
            return node, {"accepted": [n["region"] for n, _ in accepted], "rejected": rejected,
                          "selected_region": node["region"], "gpu_id": gpu["id"], "reason": reason}

    def release(self, region: str | None, gpu_id: str | None, memory_gb: float, task_id: str) -> None:
        if not region or not gpu_id:
            return
        with self.lock:
            node = self.nodes.get(region)
            if not node:
                return
            for gpu in node["gpus"]:
                if gpu["id"] == gpu_id:
                    gpu["free_gb"] = min(float(gpu["total_gb"]), float(gpu["free_gb"]) + memory_gb)
                    gpu["reserved_by"] = [x for x in gpu["reserved_by"] if x not in ("pending", task_id)]
                    gpu["utilization_pct"] = 0
                    gpu["temperature_c"] = 34
                    return

    def reserve_all(self, leave_gb: float = 0.0) -> None:
        with self.lock:
            for node in self.nodes.values():
                for gpu in node["gpus"]:
                    gpu["free_gb"] = leave_gb
                    gpu["reserved_by"] = ["backpressure"]
                    gpu["utilization_pct"] = 95
                    gpu["temperature_c"] = 78

    def reserve_hainan(self, per_gpu_reserve_gb: float = 20.0) -> None:
        with self.lock:
            for gpu in self.nodes["海南"]["gpus"]:
                gpu["free_gb"] = max(0.0, float(gpu["total_gb"]) - per_gpu_reserve_gb)
                gpu["reserved_by"] = ["dt02-load"]
                gpu["utilization_pct"] = 88
                gpu["temperature_c"] = 72

    def set_task(self, task_id: str, **updates: Any) -> dict[str, Any]:
        with self.lock:
            self.tasks[task_id].update(updates)
            return dict(self.tasks[task_id])

    def start_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = str(payload.get("scenario") or "dt01_cross_region")
        # backward-compatible aliases
        aliases = {
            "cross_region": "dt01_cross_region",
            "vram16": "dt03_vram16",
            "otn_outage": "dt04_otn_outage",
        }
        scenario = aliases.get(scenario, scenario)
        if scenario not in SCENARIOS:
            return {"error": f"unknown scenario: {scenario}"}
        meta = SCENARIOS[scenario]
        defaults = meta["defaults"]

        with self.lock:
            self.sequence += 1
            parent = str(payload.get("task_id") or f"POC-LIVE-{self.sequence:04d}")
            count = max(1, min(int(payload.get("shards", defaults["shards"])), 16))
            memory = float(payload.get("memory_gb", defaults["memory_gb"]))
            mode = str(payload.get("mode") or defaults["mode"])
            if parent in self.tasks:
                return {"error": "task_id already exists", "task_id": parent}

            if scenario == "dt05_isolation":
                created = []
                for tag in ("A", "B"):
                    pid = f"{parent}-{tag}"
                    created.append(self._create_parent_locked(pid, scenario, mode, memory, count, meta))
                    threading.Thread(target=self._run_parent, args=(pid,), daemon=True).start()
                return {"task_id": parent, "parents": created, "scenario": scenario, "message": "已并行创建A/B父任务"}

            task = self._create_parent_locked(parent, scenario, mode, memory, count, meta)
            threading.Thread(target=self._run_parent, args=(parent,), daemon=True).start()
            return dict(task)

    def _create_parent_locked(self, parent: str, scenario: str, mode: str, memory: float, count: int, meta: dict[str, Any]) -> dict[str, Any]:
        task = {
            "task_id": parent, "parent_id": parent, "type": "parent", "status": "SUBMITTED",
            "scenario": scenario, "scenario_code": meta["code"], "scenario_title": meta["title"],
            "mode": mode, "memory_gb": memory, "shards": count,
            "created_at": now_iso(), "progress": 0, "success_shards": 0, "failed_shards": 0,
            "regions": [], "result_sha256": "", "message": "已登记，等待分片调度", "cleanup_record": None,
        }
        self.tasks[parent] = task
        for i in range(1, count + 1):
            child_id = f"{parent}-S{i:02d}"
            self.tasks[child_id] = {
                "task_id": child_id, "parent_id": parent, "type": "child", "shard": i,
                "shards": count, "status": "QUEUED", "scenario": scenario, "mode": mode,
                "memory_gb": memory, "progress": 0, "selected_region": None, "gpu_id": None,
                "reason": "等待调度", "created_at": now_iso(), "updated_at": now_iso(),
                "result_sha256": "", "retry_of": None, "message": "QUEUED",
                "accepted": [], "rejected": [],
            }
        self.log(parent, "SUBMIT", f"[{meta['code']}] {meta['title']}：生成{count}个分片", shards=count, memory_gb=memory, scenario=scenario)
        return dict(task)

    def _run_parent(self, parent_id: str) -> None:
        parent = self.tasks[parent_id]
        count = int(parent["shards"])
        mode = parent["mode"]
        scenario = parent["scenario"]
        memory = float(parent["memory_gb"])
        self.set_task(parent_id, status="RUNNING", message="正在按分片实时调度")

        if scenario == "tc06_resource_discover":
            snap = [self.node_snapshot(n) for n in self.nodes.values()]
            self.log(parent_id, "RESOURCE_SNAPSHOT", "资源快照字段完整：GPU型号/数量/单卡显存/健康/链路", snapshot=snap)
            with self.lock:
                self.nodes["海南"]["gpus"][0]["utilization_pct"] = 41
                self.nodes["海南"]["gpus"][0]["free_gb"] = 18.0
            self.log(parent_id, "STATE_REFRESH", "受控改变海南 GPU1 负载后完成状态刷新", region="海南")
            digest = hashlib.sha256(f"{parent_id}|resource".encode()).hexdigest()
            self.set_task(parent_id, status="SUCCEEDED", progress=100, success_shards=1, finished_at=now_iso(),
                          result_sha256=digest, message="资源发现与刷新完成")
            self.log(parent_id, "COMPLETE", "TC06/TC07 仿真通过")
            return

        if scenario == "tc27_bad_image":
            child_id = f"{parent_id}-S01"
            self.set_task(child_id, status="UNSCHEDULED", reason="镜像摘要与冻结值不一致", message="兼容性/镜像校验失败",
                          rejected=[{"region": "海南", "reason": "镜像摘要不一致"}, {"region": "重庆", "reason": "镜像摘要不一致"}])
            self.log(child_id, "REJECT", "镜像拉取/启动前校验失败，不残留失控容器")
            self.set_task(parent_id, status="FAILED", progress=100, failed_shards=1, finished_at=now_iso(),
                          message="镜像校验失败，未派发执行")
            self.log(parent_id, "COMPLETE", "TC27 按预期失败并留痕")
            return

        if scenario == "tc12_backpressure":
            self.reserve_all(leave_gb=0.0)
            self.log(parent_id, "BACKPRESSURE", "已占满两地单卡可用显存，后续任务应排队或拒绝")

        if scenario == "dt02_divert":
            # first shard before load, second after reserving Hainan
            pass

        if scenario == "tc22_node_offline":
            with self.lock:
                self.nodes["重庆"]["healthy"] = False
            self.log(parent_id, "NODE_OFFLINE", "重庆节点标记不健康，停止派发新任务", region="重庆")

        for i in range(1, count + 1):
            child_id = f"{parent_id}-S{i:02d}"
            affinity = None
            if scenario in ("dt01_cross_region", "dt04_otn_outage", "tc01_ingress", "dt05_isolation") and i <= 2:
                affinity = "海南"
            elif scenario in ("dt01_cross_region", "dt04_otn_outage", "tc01_ingress", "dt05_isolation") and i in (3, 4):
                affinity = "重庆"

            if scenario == "dt02_divert" and i == 2:
                self.reserve_hainan(20.0)
                self.log(child_id, "LOAD_INJECT", "海南两张4090各预占约20GB，剩余显存不足以承接8GB任务")

            if scenario == "dt04_otn_outage" and i == 3:
                with self.lock:
                    self.nodes["重庆"]["link_up"] = False
                self.log(child_id, "LINK_DOWN", "模拟海南—重庆OTN中断，重庆停止接收新分片", region="重庆")

            self.set_task(child_id, status="SCHEDULING", updated_at=now_iso(), message="检查节点健康、链路、兼容性和单卡显存")
            node, decision = self.choose(memory, mode, affinity)

            if node is None and scenario == "dt04_otn_outage" and affinity == "重庆":
                self.set_task(child_id, status="FAILED-LINK", updated_at=now_iso(), reason=decision["reason"],
                              message="重庆路径不可用，准备重试", rejected=decision.get("rejected", []))
                self.log(child_id, "FAIL", "重庆分片因链路不可用失败", reason=decision["reason"])
                self.set_task(child_id, status="RETRYING", retry_of=child_id, message="关联重试至海南")
                node, decision = self.choose(memory, "海南优先", "海南")

            if node is None and scenario == "tc12_backpressure":
                self.set_task(child_id, status="QUEUED", updated_at=now_iso(), reason=decision["reason"],
                              message="资源耗尽，进入背压排队", rejected=decision.get("rejected", []))
                self.log(child_id, "QUEUE", "背压生效：任务保持可追踪排队状态，服务未崩溃")
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue

            if node is None:
                self.set_task(child_id, status="UNSCHEDULED", updated_at=now_iso(), reason=decision["reason"],
                              message="无满足硬约束的节点", rejected=decision.get("rejected", []))
                self.log(child_id, "UNSCHEDULED", decision["reason"], rejected=decision.get("rejected", []))
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue

            region = node["region"]
            gpu_id = decision["gpu_id"]
            self.set_task(child_id, status="RUNNING", selected_region=region, gpu_id=gpu_id,
                          reason=decision["reason"], accepted=decision.get("accepted", []), rejected=decision.get("rejected", []),
                          started_at=now_iso(), progress=15, message="已分配GPU，执行中")
            self.log(child_id, "DISPATCH", f"选择{region} {gpu_id}", region=region, gpu_id=gpu_id,
                     reason=decision["reason"], candidates=decision.get("accepted", []), rejected=decision.get("rejected", []))
            with self.lock:
                for gpu in node["gpus"]:
                    if gpu["id"] == gpu_id:
                        gpu["reserved_by"] = [x for x in gpu["reserved_by"] if x != "pending"] + [child_id]
                        gpu["utilization_pct"] = 82
                        gpu["temperature_c"] = 67
                        break

            if scenario == "tc18_timeout":
                time.sleep(0.4)
                self.release(region, gpu_id, memory, child_id)
                self.set_task(child_id, status="FAILED-TIMEOUT", progress=40, finished_at=now_iso(),
                              message="超过冻结超时，分类为执行失败", reason="timeout_exceeded")
                self.log(child_id, "FAIL", "超时失败，保留退出原因与日志索引")
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue

            for progress in (35, 60, 85):
                time.sleep(0.28)
                self.set_task(child_id, progress=progress, updated_at=now_iso(), message="GPU推理与结果写入中")
            time.sleep(0.2)
            digest = hashlib.sha256(f"{parent_id}|{child_id}|seed=202607".encode()).hexdigest()
            self.release(region, gpu_id, memory, child_id)
            self.set_task(child_id, status="SUCCEEDED", progress=100, finished_at=now_iso(), result_sha256=digest,
                          message="结果哈希已生成并完成分片校验")
            self.log(child_id, "SUCCEEDED", "分片执行完成，结果哈希已生成", region=region, gpu_id=gpu_id, result_sha256=digest)
            with self.lock:
                self.tasks[parent_id]["success_shards"] += 1
                self.tasks[parent_id]["progress"] = round((i / count) * 100, 1)
                if region not in self.tasks[parent_id]["regions"]:
                    self.tasks[parent_id]["regions"].append(region)

        if scenario == "dt04_otn_outage":
            with self.lock:
                self.nodes["重庆"]["link_up"] = True
            self.log(parent_id, "LINK_RECOVERED", "模拟OTN恢复，重庆重新具备候选资格")

        if scenario == "tc22_node_offline":
            with self.lock:
                self.nodes["重庆"]["healthy"] = True
            self.log(parent_id, "NODE_RECOVERED", "重庆完成健康检查后重新纳管", region="重庆")

        if scenario == "tc12_backpressure":
            with self.lock:
                for node in self.nodes.values():
                    for gpu in node["gpus"]:
                        gpu["free_gb"] = float(gpu["total_gb"])
                        gpu["reserved_by"] = []
                        gpu["utilization_pct"] = 0
                        gpu["temperature_c"] = 34
            self.log(parent_id, "CAPACITY_RESTORED", "背压演示结束，资源已释放")

        with self.lock:
            parent = self.tasks[parent_id]
            success = int(parent["success_shards"])
            failed = int(parent["failed_shards"])
            if scenario == "tc18_timeout":
                parent["status"] = "FAILED"
            elif scenario == "tc12_backpressure":
                parent["status"] = "PARTIAL" if failed else "SUCCEEDED"
            elif success == count:
                parent["status"] = "SUCCEEDED"
            elif success:
                parent["status"] = "PARTIAL"
            else:
                parent["status"] = "FAILED"
            parent["progress"] = 100 if success == count else round(success / max(1, count) * 100, 1)
            parent["finished_at"] = now_iso()
            if success == count:
                parent["result_sha256"] = hashlib.sha256(
                    "|".join(self.tasks[f"{parent_id}-S{i:02d}"].get("result_sha256", "") for i in range(1, count + 1)).encode()
                ).hexdigest()
            parent["message"] = f"{success}/{count}个分片完成，结果已汇聚"

            if scenario == "dt05_isolation" and parent["status"] == "SUCCEEDED":
                cleanup = {
                    "objects": ["input_dir", "intermediate", "temp_container", "nonessential_cache"],
                    "executor": "sim-cleaner",
                    "ts": now_iso(),
                    "result": "deleted",
                    "bound_task_id": parent_id,
                }
                parent["cleanup_record"] = cleanup
                self.log(parent_id, "CLEANUP", "结果确认后清理输入/中间文件/临时容器/非必要缓存", cleanup=cleanup)
                self.log(parent_id, "RETURN", "模拟经VPN回传新加坡，SHA-256一致", result_sha256=parent["result_sha256"])

        self.log(parent_id, "COMPLETE", self.tasks[parent_id]["message"], status=self.tasks[parent_id]["status"])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tasks = [dict(v) for v in self.tasks.values()]
            tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            parents = [x for x in tasks if x.get("type") == "parent"]
            counts = {key: sum(1 for x in tasks if x.get("status") == key) for key in
                      ("SUCCEEDED", "RUNNING", "QUEUED", "FAILED-LINK", "UNSCHEDULED", "FAILED-TIMEOUT", "FAILED", "PARTIAL")}
            return {
                "mode": SIMULATION_MODE,
                "reality": {
                    "scheduler": "simulation",
                    "note": "本控制台为规则仿真，不驱动海南/重庆真实GPU任务；三地80/8000多为连通性探针，8080上新加坡为本UI，海南另有业务Web。",
                    "ports": {
                        "singapore_ui": "43.106.50.98:8080",
                        "connectivity_probe": "三地 TCP 80/8000/8080（部分为探针/隧道）",
                        "monitoring": "node_exporter / DCGM / Prometheus（各机本地监控端口，非本UI调度口）",
                    },
                },
                "server_time": now_iso(),
                "scenarios": list(SCENARIOS.values()),
                "nodes": [self.node_snapshot(n) for n in self.nodes.values()],
                "tasks": tasks[:120], "parents": parents[:40], "events": list(self.events[:100]),
                "metrics": list(self.metrics[-120:]),
                "stats": {"total": len(parents), "succeeded": sum(1 for x in parents if x["status"] == "SUCCEEDED"), **counts},
            }


STATE = SchedulerState()


class Handler(BaseHTTPRequestHandler):
    server_version = "L1SchedulerUI/1.0"

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
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {TOKEN}"

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object expected")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if path == "/api/health":
                self.send_json({"ok": True, "mode": SIMULATION_MODE, "auth_required": bool(TOKEN)})
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
                status = HTTPStatus.BAD_REQUEST if "error" in result and "already exists" not in result.get("error", "") else (
                    HTTPStatus.CONFLICT if "error" in result else HTTPStatus.ACCEPTED
                )
                self.send_json(result, status)
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
                with STATE.lock:
                    STATE.nodes[region][field] = not STATE.nodes[region][field]
                    value = STATE.nodes[region][field]
                STATE.log("SYSTEM", "NODE_TOGGLE", f"{region} {field}={value}", region=region, field=field, value=value)
                self.send_json({"ok": True, "region": region, "field": field, "value": value})
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "application/javascript; charset=utf-8"
        raw = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="L1 scheduler real-time visualization")
    parser.add_argument("--host", default=os.environ.get("SCHEDULER_UI_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SCHEDULER_UI_PORT", "8080")))
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"L1 scheduler UI listening on http://{args.host}:{args.port} mode={SIMULATION_MODE} auth={'on' if TOKEN else 'off'}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
