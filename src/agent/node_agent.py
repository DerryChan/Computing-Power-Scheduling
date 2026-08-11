#!/usr/bin/env python3
"""L1 real node agent with ResNet PoC workload + CUDA fallback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
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
TOKEN = os.environ.get("L1_AGENT_TOKEN", "").strip()
REGION = os.environ.get("L1_AGENT_REGION", "UNKNOWN")
WORKER = Path(os.environ.get("L1_GPU_WORKER", str(ROOT / "gpu_worker")))
BUNDLE = Path(os.environ.get("L1_POC_BUNDLE", "/opt/l1-poc-bundle"))
PYTHON = os.environ.get("L1_PYTHON", "python3")
WORK_DIR = Path(os.environ.get("L1_AGENT_WORK", str(ROOT / "work")))
WORK_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run_cmd(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def nvidia_query() -> list[dict[str, Any]]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.free,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = run_cmd(cmd, timeout=10)
    except Exception as exc:
        return [{"error": f"nvidia-smi failed: {exc}"}]
    if proc.returncode != 0:
        return [{"error": proc.stderr.strip() or "nvidia-smi error"}]
    gpus = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        idx = int(parts[0])
        total = float(parts[3]); free = float(parts[4]); used = float(parts[5])
        gpus.append({
            "id": f"{REGION}-GPU{idx + 1}", "index": idx, "uuid": parts[1], "model": parts[2],
            "total_gb": round(total / 1024.0, 2), "free_gb": round(free / 1024.0, 2),
            "used_gb": round(used / 1024.0, 2), "utilization_pct": float(parts[6]),
            "temperature_c": float(parts[7]),
            "load_pct": round((used / total) * 100, 1) if total else 0.0,
        })
    return gpus


class AgentState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.healthy = True
        self.link_up = True
        self.events: list[dict[str, Any]] = []

    def log(self, message: str, **extra: Any) -> None:
        item = {"ts": now_iso(), "message": message, **extra}
        with self.lock:
            self.events.insert(0, item)
            self.events = self.events[:120]

    def resources(self) -> dict[str, Any]:
        gpus = [g for g in nvidia_query() if "index" in g]
        with self.lock:
            busy = {t["gpu_index"] for t in self.tasks.values()
                    if t.get("status") in ("RUNNING", "STARTING") and t.get("gpu_index") is not None}
        for g in gpus:
            g["busy"] = g["index"] in busy
            g["reserved_by"] = [t["task_id"] for t in self.tasks.values()
                                if t.get("gpu_index") == g["index"] and t.get("status") in ("RUNNING", "STARTING")]
        return {
            "region": REGION, "healthy": self.healthy, "link_up": self.link_up,
            "server_time": now_iso(), "gpus": gpus, "mode": "real",
            "bundle": str(BUNDLE), "bundle_ready": (BUNDLE / "weights" / "resnet50_frozen.pth").exists(),
            "python": PYTHON,
        }

    def pick_gpu(self, memory_gb: float) -> dict[str, Any] | None:
        gpus = [g for g in nvidia_query() if "index" in g]
        with self.lock:
            busy = {t["gpu_index"] for t in self.tasks.values()
                    if t.get("status") in ("RUNNING", "STARTING") and t.get("gpu_index") is not None}
        candidates = [g for g in gpus if g["index"] not in busy and float(g["free_gb"]) >= memory_gb]
        if not candidates:
            candidates = [g for g in gpus if float(g["free_gb"]) >= memory_gb]
        if not candidates:
            return None
        return max(candidates, key=lambda g: float(g["free_gb"]))

    def start_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.healthy:
            return {"error": "node unhealthy", "code": "NODE_UNHEALTHY"}
        if not self.link_up:
            return {"error": "link down", "code": "LINK_DOWN"}

        task_id = str(payload.get("task_id") or f"T-{uuid.uuid4().hex[:8]}")
        memory_gb = float(payload.get("memory_gb", 8))
        duration = int(payload.get("duration_sec", 4))
        parent_id = str(payload.get("parent_id") or task_id)
        scenario = str(payload.get("scenario") or "")
        workload = str(payload.get("workload") or "resnet")  # resnet|cuda|gpu_load|vram_test
        shard_manifest = str(payload.get("shard_manifest") or "")
        force_fail = bool(payload.get("force_fail", False))
        bad_image = bool(payload.get("bad_image", False))

        with self.lock:
            if task_id in self.tasks and self.tasks[task_id].get("status") in ("RUNNING", "STARTING", "QUEUED"):
                return {"error": "task already running", "task_id": task_id, "code": "DUPLICATE"}

        if bad_image:
            task = {
                "task_id": task_id, "parent_id": parent_id, "status": "FAILED",
                "message": "镜像摘要与冻结值不一致", "reason": "bad_image",
                "created_at": now_iso(), "finished_at": now_iso(), "progress": 100,
                "memory_gb": memory_gb, "scenario": scenario, "result_sha256": "",
            }
            with self.lock:
                self.tasks[task_id] = task
            return dict(task)

        gpu = self.pick_gpu(memory_gb)
        if gpu is None:
            task = {
                "task_id": task_id, "parent_id": parent_id, "status": "UNSCHEDULED",
                "message": "单卡可用显存不足或GPU繁忙", "reason": "no_gpu",
                "created_at": now_iso(), "finished_at": now_iso(), "progress": 100,
                "memory_gb": memory_gb, "scenario": scenario, "result_sha256": "",
            }
            with self.lock:
                self.tasks[task_id] = task
            return dict(task)

        out_dir = WORK_DIR / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        task = {
            "task_id": task_id, "parent_id": parent_id, "status": "STARTING",
            "message": f"starting {workload}", "created_at": now_iso(), "updated_at": now_iso(),
            "progress": 5, "memory_gb": memory_gb, "duration_sec": duration,
            "gpu_index": gpu["index"], "gpu_id": gpu["id"], "device": gpu["model"],
            "scenario": scenario, "workload": workload, "shard_manifest": shard_manifest,
            "result_sha256": "", "out_dir": str(out_dir), "log_file": str(out_dir / "stdout.log"),
            "pid": None, "force_fail": force_fail, "cancel_requested": False,
        }
        with self.lock:
            self.tasks[task_id] = task
        threading.Thread(target=self._run_task, args=(task_id,), daemon=True).start()
        return dict(task)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return {"error": "not found"}
            task["cancel_requested"] = True
            pid = task.get("pid")
        if pid:
            try:
                os.kill(int(pid), 15)
            except Exception:
                pass
        with self.lock:
            self.tasks[task_id].update({
                "status": "CANCELLED", "progress": 100, "finished_at": now_iso(),
                "message": "任务已取消", "reason": "cancelled",
            })
        return dict(self.tasks[task_id])

    def _run_task(self, task_id: str) -> None:
        with self.lock:
            task = dict(self.tasks[task_id])
        if task.get("force_fail"):
            time.sleep(1.0)
            with self.lock:
                self.tasks[task_id].update({
                    "status": "FAILED-TIMEOUT", "progress": 40, "finished_at": now_iso(),
                    "message": "超过冻结超时，分类为执行失败", "reason": "timeout_exceeded",
                })
            return

        workload = task.get("workload") or "resnet"
        gpu_index = int(task["gpu_index"])
        out_dir = Path(task["out_dir"])
        log_file = Path(task["log_file"])
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)

        if workload == "resnet":
            cmd = self._resnet_cmd(task, gpu_visible=0)
        elif workload == "gpu_load":
            cmd = [PYTHON, str(BUNDLE / "app" / "gpu_load.py"),
                   "--reserve-mb", str(int(float(task["memory_gb"]) * 1024)),
                   "--duration", str(task["duration_sec"]), "--gpu", "0", "--gemm"]
        elif workload == "vram_test":
            cmd = [PYTHON, str(BUNDLE / "app" / "vram_test.py"),
                   "--allocate-mb", str(int(float(task["memory_gb"]) * 1024 * 0.9)),
                   "--iterations", "200", "--seed", "202607", "--gpu", "0"]
        else:
            # cuda worker fallback
            if not WORKER.is_file():
                with self.lock:
                    self.tasks[task_id].update({"status": "FAILED", "message": "gpu_worker missing", "progress": 100, "finished_at": now_iso()})
                return
            mem_mb = max(256, int(float(task["memory_gb"]) * 1024 * 0.85))
            cmd = [str(WORKER), "--gpu", "0", "--memory-mb", str(mem_mb),
                   "--duration-sec", str(task["duration_sec"]), "--seed", "202607",
                   "--task-id", task_id, "--out", str(out_dir / "result.json")]

        if cmd is None:
            with self.lock:
                self.tasks[task_id].update({
                    "status": "FAILED", "progress": 100, "finished_at": now_iso(),
                    "message": "resnet bundle incomplete", "reason": "bundle_missing",
                })
            return

        with self.lock:
            self.tasks[task_id].update({"status": "RUNNING", "progress": 15, "message": f"{workload} running", "updated_at": now_iso()})
        self.log("task_start", task_id=task_id, workload=workload, gpu=gpu_index)

        try:
            with open(log_file, "w", encoding="utf-8") as lf:
                proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, cwd=str(out_dir))
            with self.lock:
                self.tasks[task_id]["pid"] = proc.pid
            while proc.poll() is None:
                time.sleep(0.5)
                with self.lock:
                    if self.tasks[task_id].get("cancel_requested"):
                        break
                    p = min(90, int(self.tasks[task_id].get("progress", 15)) + 5)
                    self.tasks[task_id]["progress"] = p
                    self.tasks[task_id]["updated_at"] = now_iso()
            rc = proc.returncode
            if self.tasks[task_id].get("cancel_requested"):
                return
            stdout = log_file.read_text(encoding="utf-8", errors="ignore") if log_file.exists() else ""
            result_sha = ""
            metrics = {}
            if workload == "resnet":
                pred = out_dir / "predictions.jsonl"
                met = out_dir / "metrics.json"
                if met.exists():
                    metrics = json.loads(met.read_text(encoding="utf-8"))
                    result_sha = metrics.get("predictions_sha256") or ""
                if not result_sha and pred.exists():
                    result_sha = hashlib.sha256(pred.read_bytes()).hexdigest()
                ok = rc == 0 and pred.exists()
            else:
                # parse last json
                for line in reversed(stdout.splitlines()):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            metrics = json.loads(line)
                            break
                        except Exception:
                            pass
                if (out_dir / "result.json").exists():
                    metrics = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
                result_sha = metrics.get("result_sha256") or hashlib.sha256(stdout.encode()).hexdigest()
                ok = rc == 0 and bool(metrics.get("ok", True))

            if ok:
                with self.lock:
                    self.tasks[task_id].update({
                        "status": "SUCCEEDED", "progress": 100, "finished_at": now_iso(),
                        "result_sha256": result_sha, "metrics": metrics,
                        "message": f"{workload} completed", "updated_at": now_iso(),
                        "output_files": [str(p.relative_to(out_dir)) for p in out_dir.iterdir()],
                    })
                self.log("task_success", task_id=task_id, result_sha256=result_sha)
            else:
                with self.lock:
                    self.tasks[task_id].update({
                        "status": "FAILED", "progress": 100, "finished_at": now_iso(),
                        "message": f"{workload} rc={rc}", "reason": "worker_failed",
                        "log_tail": stdout[-1500:], "updated_at": now_iso(),
                    })
        except Exception as exc:
            with self.lock:
                self.tasks[task_id].update({
                    "status": "FAILED", "progress": 100, "finished_at": now_iso(),
                    "message": str(exc), "reason": "exception", "updated_at": now_iso(),
                })

    def _resnet_cmd(self, task: dict[str, Any], gpu_visible: int = 0) -> list[str] | None:
        weights = BUNDLE / "weights" / "resnet50_frozen.pth"
        data_root = BUNDLE / "data" / "dataset-140m"
        infer = BUNDLE / "app" / "infer.py"
        if not (weights.exists() and data_root.exists() and infer.exists()):
            return None
        shard_manifest = task.get("shard_manifest") or ""
        if not shard_manifest:
            # default first shard
            shard_manifest = str(data_root / "manifest_part-01.csv")
        elif not Path(shard_manifest).is_absolute():
            shard_manifest = str(data_root / shard_manifest)
        out_dir = Path(task["out_dir"])
        return [
            PYTHON, str(infer),
            "--manifest", shard_manifest,
            "--data-root", str(data_root),
            "--weights", str(weights),
            "--output", str(out_dir),
            "--batch-size", "64",
            "--seed", "202607",
            "--deterministic",
            "--gpu", str(gpu_visible),
        ]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            t = self.tasks.get(task_id)
            return dict(t) if t else None

    def fetch_output(self, task_id: str, name: str) -> bytes | None:
        with self.lock:
            t = self.tasks.get(task_id)
            if not t:
                return None
            out_dir = Path(t.get("out_dir") or "")
        path = (out_dir / name).resolve()
        if not str(path).startswith(str(out_dir.resolve())) or not path.is_file():
            return None
        return path.read_bytes()

    def cleanup(self, task_id: str | None = None) -> dict[str, Any]:
        removed = []
        with self.lock:
            ids = [task_id] if task_id else list(self.tasks.keys())
            for tid in ids:
                t = self.tasks.get(tid)
                if not t:
                    continue
                out_dir = Path(t.get("out_dir") or "")
                if out_dir.exists():
                    for p in out_dir.rglob("*"):
                        if p.is_file():
                            removed.append(str(p))
                            p.unlink(missing_ok=True)
                    shutil.rmtree(out_dir, ignore_errors=True)
                t["cleanup_record"] = {"ts": now_iso(), "removed_count": len(removed), "result": "deleted",
                                       "objects": ["output_dir", "predictions", "metrics", "stdout.log"]}
        self.log("cleanup", task_id=task_id or "ALL", removed=len(removed))
        return {"ok": True, "removed_count": len(removed), "ts": now_iso(), "executor": f"agent-{REGION}"}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tasks = [dict(v) for v in self.tasks.values()]
        tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {"region": REGION, "mode": "real", "resources": self.resources(),
                "tasks": tasks[:80], "events": list(self.events[:50]), "server_time": now_iso()}


STATE = AgentState()


class Handler(BaseHTTPRequestHandler):
    server_version = "L1NodeAgent/2.0"

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
        if path in ("/", "/health"):
            self.send_json({"ok": True, "region": REGION, "mode": "real", "auth_required": bool(TOKEN)})
            return
        if not self.authorized():
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if path in ("/v1/resources", "/resources"):
            self.send_json(STATE.resources()); return
        if path in ("/v1/status", "/status"):
            self.send_json(STATE.snapshot()); return
        m = re.match(r"^/v1/tasks/([^/]+)/files/(.+)$", path)
        if m:
            data = STATE.fetch_output(m.group(1), m.group(2))
            if data is None:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND); return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        m = re.match(r"^/v1/tasks/([^/]+)$", path)
        if m:
            task = STATE.get_task(m.group(1))
            if not task:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND); return
            self.send_json(task); return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # auth endpoints still require token except health
        if not self.authorized():
            self.send_json({"error": "unauthorized", "source": self.client_address[0], "path": path, "ts": now_iso()},
                           HTTPStatus.UNAUTHORIZED)
            STATE.log("auth_reject", path=path, source=self.client_address[0])
            return
        try:
            payload = self.read_json()
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST); return
        if path in ("/v1/tasks", "/tasks"):
            result = STATE.start_task(payload)
            code = HTTPStatus.ACCEPTED if "error" not in result else HTTPStatus.CONFLICT
            if result.get("status") in ("UNSCHEDULED", "FAILED") and "error" not in result:
                code = HTTPStatus.OK
            self.send_json(result, code); return
        m = re.match(r"^/v1/tasks/([^/]+)/cancel$", path)
        if m:
            self.send_json(STATE.cancel_task(m.group(1))); return
        if path in ("/v1/cleanup", "/cleanup"):
            self.send_json(STATE.cleanup(payload.get("task_id"))); return
        if path in ("/v1/admin/toggle", "/admin/toggle"):
            field = str(payload.get("field"))
            if field not in ("healthy", "link_up"):
                self.send_json({"error": "invalid field"}, HTTPStatus.BAD_REQUEST); return
            with STATE.lock:
                setattr(STATE, field, not getattr(STATE, field))
                value = getattr(STATE, field)
            STATE.log("toggle", field=field, value=value)
            self.send_json({"ok": True, "field": field, "value": value}); return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("L1_AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("L1_AGENT_PORT", "8000")))
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"L1 node agent region={REGION} http://{args.host}:{args.port} bundle={BUNDLE} py={PYTHON}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
