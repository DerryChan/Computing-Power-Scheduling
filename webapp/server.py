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
        self.sequence = 0
        self.nodes: dict[str, dict[str, Any]] = {}
        self.reset()

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
            self.sequence = 0
            self.log("SYSTEM", "RESET", "仿真调度器已重置")

    def log(self, task_id: str, event: str, message: str, **extra: Any) -> None:
        item = {"ts": now_iso(), "task_id": task_id, "event": event, "message": message, **extra}
        with self.lock:
            self.events.insert(0, item)
            self.events = self.events[:250]

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

    def set_task(self, task_id: str, **updates: Any) -> dict[str, Any]:
        with self.lock:
            self.tasks[task_id].update(updates)
            return dict(self.tasks[task_id])

    def start_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.sequence += 1
            parent = str(payload.get("task_id") or f"POC-LIVE-{self.sequence:04d}")
            count = max(1, min(int(payload.get("shards", 8)), 16))
            memory = float(payload.get("memory_gb", 8))
            mode = str(payload.get("mode") or "海南优先")
            scenario = str(payload.get("scenario") or "cross_region")
            if parent in self.tasks:
                return {"error": "task_id already exists", "task_id": parent}
            task = {
                "task_id": parent, "parent_id": parent, "type": "parent", "status": "SUBMITTED",
                "scenario": scenario, "mode": mode, "memory_gb": memory, "shards": count,
                "created_at": now_iso(), "progress": 0, "success_shards": 0, "failed_shards": 0,
                "regions": [], "result_sha256": "", "message": "已登记，等待分片调度",
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
                }
            self.log(parent, "SUBMIT", f"父任务已提交，生成{count}个分片", shards=count, memory_gb=memory, scenario=scenario)
            thread = threading.Thread(target=self._run_parent, args=(parent,), daemon=True)
            thread.start()
            return dict(task)

    def _run_parent(self, parent_id: str) -> None:
        parent = self.tasks[parent_id]
        count = int(parent["shards"])
        mode = parent["mode"]
        scenario = parent["scenario"]
        memory = float(parent["memory_gb"])
        self.set_task(parent_id, status="RUNNING", message="正在按分片实时调度")
        for i in range(1, count + 1):
            child_id = f"{parent_id}-S{i:02d}"
            affinity = None
            if scenario in ("cross_region", "otn_outage") and i <= 2:
                affinity = "海南"
            elif scenario in ("cross_region", "otn_outage") and i in (3, 4):
                affinity = "重庆"
            if scenario == "otn_outage" and i == 3:
                with self.lock:
                    self.nodes["重庆"]["link_up"] = False
                self.log(child_id, "LINK_DOWN", "模拟海南—重庆OTN中断，重庆停止接收新分片", region="重庆")
            self.set_task(child_id, status="SCHEDULING", updated_at=now_iso(), message="检查节点健康、链路和单卡显存")
            node, decision = self.choose(memory, mode, affinity)
            if node is None and scenario == "otn_outage" and affinity == "重庆":
                self.set_task(child_id, status="FAILED-LINK", updated_at=now_iso(), reason=decision["reason"],
                              message="重庆路径不可用，准备重试", rejected=decision.get("rejected", []))
                self.log(child_id, "FAIL", "重庆分片因链路不可用失败", reason=decision["reason"])
                self.set_task(child_id, status="RETRYING", retry_of=child_id, message="关联重试至海南")
                node, decision = self.choose(memory, "海南优先", "海南")
            if node is None:
                self.set_task(child_id, status="UNSCHEDULED", updated_at=now_iso(), reason=decision["reason"],
                              message="无满足硬约束的节点", rejected=decision.get("rejected", []))
                self.log(child_id, "UNSCHEDULED", decision["reason"])
                with self.lock:
                    self.tasks[parent_id]["failed_shards"] += 1
                continue
            region = node["region"]
            gpu_id = decision["gpu_id"]
            self.set_task(child_id, status="RUNNING", selected_region=region, gpu_id=gpu_id,
                          reason=decision["reason"], accepted=decision.get("accepted", []), rejected=decision.get("rejected", []),
                          started_at=now_iso(), progress=15, message="已分配GPU，执行中")
            self.log(child_id, "DISPATCH", f"选择{region} {gpu_id}", region=region, gpu_id=gpu_id,
                     reason=decision["reason"], candidates=decision.get("accepted", []))
            with self.lock:
                for gpu in node["gpus"]:
                    if gpu["id"] == gpu_id:
                        gpu["reserved_by"] = [x for x in gpu["reserved_by"] if x != "pending"] + [child_id]
                        gpu["utilization_pct"] = 82
                        gpu["temperature_c"] = 67
                        break
            for progress in (35, 60, 85):
                time.sleep(0.35 if scenario != "slow" else 0.8)
                self.set_task(child_id, progress=progress, updated_at=now_iso(), message="GPU推理与结果写入中")
            time.sleep(0.25)
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
        if scenario == "otn_outage":
            with self.lock:
                self.nodes["重庆"]["link_up"] = True
            self.log(parent_id, "LINK_RECOVERED", "模拟OTN恢复，重庆重新具备候选资格")
        with self.lock:
            parent = self.tasks[parent_id]
            success = int(parent["success_shards"])
            parent["status"] = "SUCCEEDED" if success == count else ("PARTIAL" if success else "FAILED")
            parent["progress"] = 100 if success == count else round(success / count * 100, 1)
            parent["finished_at"] = now_iso()
            parent["result_sha256"] = hashlib.sha256("|".join(self.tasks[f"{parent_id}-S{i:02d}"]["result_sha256"] for i in range(1, count + 1)).encode()).hexdigest() if success == count else ""
            parent["message"] = f"{success}/{count}个分片完成，结果已汇聚"
        self.log(parent_id, "COMPLETE", self.tasks[parent_id]["message"], status=self.tasks[parent_id]["status"])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tasks = [dict(v) for v in self.tasks.values()]
            tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            parents = [x for x in tasks if x.get("type") == "parent"]
            counts = {key: sum(1 for x in tasks if x.get("status") == key) for key in ("SUCCEEDED", "RUNNING", "QUEUED", "FAILED-LINK", "UNSCHEDULED")}
            return {
                "mode": SIMULATION_MODE,
                "server_time": now_iso(),
                "nodes": [self.node_snapshot(n) for n in self.nodes.values()],
                "tasks": tasks[:80], "parents": parents[:30], "events": list(self.events[:80]),
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
            if not self.authorized():
                self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            if path in ("/api/status", "/api/tasks", "/api/events"):
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
                self.send_json(result, HTTPStatus.CONFLICT if "error" in result else HTTPStatus.ACCEPTED)
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
        if STATIC not in target.parents or not target.is_file():
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
