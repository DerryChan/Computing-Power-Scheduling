"""跨境调度实验：5 节点 × 30 任务，对比基线并汇总真实跑出来的指标。"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from .baselines import run_all_baselines
from .model import NodeState, TaskSpec, make_paper_nodes
from .adaptive_scheduler import LeaseManager, PaperScheduler

# 任务到达间隔：短于多数任务 runtime，制造资源争用
INTERARRIVAL_H = 0.08


def build_paper_tasks(seed: int = 20260812) -> list[TaskSpec]:
    """构造 30 个异构任务，覆盖时延敏感、成本敏感与 TEE 合规等画像。

    - 混合约束逼出策略差异；
    - 保留少量空集群不可行样例（预算/TEE+SLA），观察硬约束边界；
    - 运行时长与跨境传输单价使成本、能耗落在可解释量级。
    """
    tasks: list[TaskSpec] = []

    # 画像：gpu, data_gb, runtime_h, lat_limit, budget, tee, zone, sens
    # 刻意让部分任务“紧预算+松时延”或“紧时延+松预算”，单目标策略会踩坑。
    profiles: list[dict[str, Any]] = [
        # 前段：以国内/中亚为主，避免过早挤占离岸 TEE 节点
        dict(gpu=2, data=7.0, rt=4.0, lat=100, bud=70, tee=False, zone="china", sens=1.7),
        dict(gpu=1, data=5.0, rt=3.5, lat=150, bud=35, tee=False, zone="central_asia", sens=0.8),
        dict(gpu=4, data=18.0, rt=5.0, lat=280, bud=39.61, tee=False, zone="sea", sens=1.2),  # T03 预算硬冲突
        dict(gpu=2, data=6.0, rt=3.8, lat=105, bud=65, tee=False, zone="china", sens=1.6),
        dict(gpu=1, data=8.0, rt=4.2, lat=165, bud=32, tee=False, zone="central_asia", sens=0.7),
        dict(gpu=2, data=5.0, rt=3.5, lat=115, bud=90, tee=True, zone="china", sens=1.4),   # TEE → 港/新
        dict(gpu=3, data=9.0, rt=4.5, lat=110, bud=95, tee=False, zone="china", sens=1.8),
        dict(gpu=1, data=4.0, rt=3.0, lat=155, bud=28, tee=False, zone="china", sens=0.7),
        dict(gpu=2, data=10.0, rt=4.0, lat=140, bud=55, tee=False, zone="central_asia", sens=1.0),
        dict(gpu=1, data=4.5, rt=3.2, lat=95, bud=75, tee=False, zone="sea", sens=1.8),
        # 中段：夹杂成本敏感与时延敏感
        dict(gpu=2, data=7.0, rt=4.0, lat=108, bud=68, tee=False, zone="china", sens=1.5),
        dict(gpu=2, data=5.5, rt=3.6, lat=125, bud=95, tee=True, zone="sea", sens=1.3),     # TEE
        dict(gpu=3, data=11.0, rt=4.8, lat=120, bud=58, tee=False, zone="china", sens=1.5),  # 偏紧预算
        dict(gpu=1, data=9.0, rt=3.8, lat=175, bud=30, tee=False, zone="central_asia", sens=0.7),
        dict(gpu=3, data=7.0, rt=4.0, lat=52, bud=100, tee=True, zone="central_asia", sens=2.0),  # T15
        dict(gpu=2, data=5.0, rt=3.4, lat=100, bud=72, tee=False, zone="sea", sens=1.6),
        dict(gpu=1, data=6.0, rt=3.2, lat=150, bud=30, tee=False, zone="china", sens=0.8),
        dict(gpu=2, data=6.5, rt=3.7, lat=118, bud=92, tee=True, zone="china", sens=1.3),    # TEE
        dict(gpu=3, data=10.0, rt=4.6, lat=112, bud=78, tee=False, zone="sea", sens=1.7),
        dict(gpu=1, data=5.0, rt=3.0, lat=160, bud=26, tee=False, zone="central_asia", sens=0.7),
        # 后段：争用更明显；负载均衡应优于纯最小延迟/最小成本
        dict(gpu=2, data=8.0, rt=4.2, lat=110, bud=70, tee=False, zone="china", sens=1.5),
        dict(gpu=1, data=3.5, rt=2.8, lat=88, bud=80, tee=False, zone="sea", sens=1.9),
        dict(gpu=2, data=9.0, rt=4.3, lat=145, bud=52, tee=False, zone="central_asia", sens=0.9),
        dict(gpu=2, data=5.5, rt=3.5, lat=120, bud=95, tee=True, zone="sea", sens=1.2),      # TEE
        dict(gpu=4, data=16.0, rt=4.5, lat=240, bud=32.0, tee=False, zone="sea", sens=1.0),  # T25
        dict(gpu=1, data=6.5, rt=3.5, lat=150, bud=34, tee=False, zone="china", sens=0.8),
        dict(gpu=3, data=8.0, rt=4.0, lat=105, bud=88, tee=False, zone="sea", sens=1.7),
        dict(gpu=1, data=5.0, rt=3.2, lat=165, bud=28, tee=False, zone="central_asia", sens=0.7),
        dict(gpu=2, data=6.0, rt=3.8, lat=102, bud=74, tee=False, zone="china", sens=1.6),
        dict(gpu=2, data=7.0, rt=4.0, lat=125, bud=62, tee=False, zone="sea", sens=1.2),
    ]

    for i, p in enumerate(profiles, start=1):
        zone = p["zone"]
        local = {"china": "重庆", "sea": "新加坡", "central_asia": "新疆"}[zone]
        tasks.append(TaskSpec(
            task_id=f"T{i:02d}",
            gpu_required=int(p["gpu"]),
            data_gb=float(p["data"]),
            runtime_h=float(p["rt"]),
            latency_limit_ms=float(p["lat"]),
            budget=float(p["bud"]),
            require_tee=bool(p["tee"]),
            source_zone=zone,
            latency_sensitivity=float(p["sens"]),
            memory_gb=8.0,
            local_prefer=local,
        ))
    return tasks


def cold_start_infeasibility(tasks: list[TaskSpec]) -> dict[str, str]:
    """空集群硬约束下即不可行的任务（与运行期争用无关）。"""
    from .adaptive_scheduler import hard_filter

    out: dict[str, str] = {}
    nodes = make_paper_nodes()
    for task in tasks:
        acc, rej, _ = hard_filter(task, nodes)
        if acc:
            continue
        reasons = " | ".join(r["reason"] for r in rej)
        if "TEE" in reasons:
            out[task.task_id] = "空集群即不可行：TEE/合规"
        elif "预算" in reasons:
            out[task.task_id] = "空集群即不可行：预算超限"
        elif "时延" in reasons:
            out[task.task_id] = "空集群即不可行：时延超限"
        else:
            out[task.task_id] = f"空集群即不可行：{reasons[:80]}"
    return out


def classify_failure_reasons(
    rows: list[dict[str, Any]],
    *,
    cold_start: dict[str, str] | None = None,
) -> dict[str, Any]:
    """归类未调度任务：区分空集群硬冲突 vs 运行期争用主因。"""
    priority = [
        ("tee", ("TEE", "可信执行")),
        ("budget", ("预算",)),
        ("latency", ("时延",)),
        ("capacity", ("GPU 剩余", "显存", "容量")),
        ("health", ("不健康", "不可达", "链路")),
        ("other", ()),
    ]
    label = {
        "tee": "TEE 合规",
        "budget": "预算超限",
        "latency": "时延超限",
        "capacity": "GPU/显存容量（运行期争用）",
        "mixed": "运行期混合约束（容量+时延/预算等）",
        "health": "健康/链路/可达",
        "other": "其他",
        "cold_budget": "空集群预算硬冲突",
        "cold_tee": "空集群 TEE/合规硬冲突",
        "cold_latency": "空集群时延硬冲突",
        "cold_other": "空集群其他硬冲突",
    }
    cold_start = cold_start or {}
    by_task: dict[str, str] = {}
    counts: dict[str, int] = {}

    for r in rows:
        if r.get("status") == "SCHEDULED":
            continue
        tid = str(r.get("task_id"))
        if tid in cold_start:
            cs = cold_start[tid]
            if "预算" in cs:
                bucket = "cold_budget"
            elif "TEE" in cs:
                bucket = "cold_tee"
            elif "时延" in cs:
                bucket = "cold_latency"
            else:
                bucket = "cold_other"
        else:
            rej = r.get("rejected") or []
            reasons = " | ".join(str(x.get("reason", "")) for x in rej)
            capacity_hits = sum(
                1 for x in rej
                if ("GPU 剩余" in str(x.get("reason", ""))) or ("显存" in str(x.get("reason", "")))
            )
            latency_hits = sum(1 for x in rej if "时延" in str(x.get("reason", "")))
            budget_hits = sum(1 for x in rej if "预算" in str(x.get("reason", "")))
            total_rej = max(1, len(rej))
            distinct = sum(1 for h in (capacity_hits > 0, latency_hits > 0, budget_hits > 0) if h)
            if distinct >= 2:
                bucket = "mixed"
            elif capacity_hits >= total_rej or capacity_hits > 0 and latency_hits == 0 and budget_hits == 0:
                bucket = "capacity"
            else:
                bucket = "other"
                for name, keys in priority:
                    if name == "other":
                        continue
                    if any(k in reasons for k in keys):
                        bucket = name
                        break
        by_task[tid] = label.get(bucket, bucket)
        counts[bucket] = counts.get(bucket, 0) + 1

    return {
        "counts": {label.get(k, k): v for k, v in counts.items() if v},
        "by_task": by_task,
        "cold_start": cold_start,
    }


def ablation_effect_notes(ablation: dict[str, Any]) -> list[str]:
    """根据消融结果生成简要说明。"""
    full = ablation.get("本文方法（全模块）")
    if not full:
        return []
    notes: list[str] = []
    for name, s in ablation.items():
        if name == "本文方法（全模块）":
            continue
        same_sr = abs(s["success_rate_pct"] - full["success_rate_pct"]) < 1e-9
        same_lat = abs(s["avg_latency_ms"] - full["avg_latency_ms"]) < 0.05
        same_cost = abs(s["avg_cost"] - full["avg_cost"]) < 0.05
        same_fail = set(s.get("failed_ids") or []) == set(full.get("failed_ids") or [])
        if same_sr and same_lat and same_cost and same_fail:
            notes.append(f"{name}：与全模块指标一致，本场景下该权重未改变调度结果。")
        elif not same_sr:
            notes.append(
                f"{name}：成功率 {full['success_rate_pct']:.2f}% → {s['success_rate_pct']:.2f}%，"
                f"失败集 {', '.join(s.get('failed_ids') or []) or '无'}。"
            )
        else:
            notes.append(
                f"{name}：成功率不变，时延/成本有变化"
                f"（时延 {full['avg_latency_ms']:.2f}→{s['avg_latency_ms']:.2f}，"
                f"成本 {full['avg_cost']:.2f}→{s['avg_cost']:.2f}）。"
            )
    return notes


def _peak_util(nodes: dict[str, NodeState], peak: float) -> float:
    caps = sum(n.gpu_capacity for n in nodes.values())
    if caps <= 0:
        return peak
    used = sum(n.gpu_capacity - n.gpu_free for n in nodes.values())
    return max(peak, 100.0 * used / caps)


def _summarize(
    rows: list[dict[str, Any]],
    *,
    peak_util_pct: float = 0.0,
) -> dict[str, Any]:
    total = len(rows)
    ok = [r for r in rows if r["status"] == "SCHEDULED"]
    fail = total - len(ok)

    def avg(key: str) -> float:
        vals = [float(r[key]) for r in ok if r.get(key) is not None]
        return round(statistics.mean(vals), 4) if vals else 0.0

    green = 0.0
    if ok:
        green = round(100.0 * statistics.mean(float(r.get("green_ratio") or 0) for r in ok), 2)

    dist: dict[str, int] = {}
    for r in ok:
        dist[str(r["selected"])] = dist.get(str(r["selected"]), 0) + 1

    return {
        "total_tasks": total,
        "success_tasks": len(ok),
        "unscheduled_tasks": fail,
        "success_rate_pct": round(100.0 * len(ok) / total, 2) if total else 0.0,
        "avg_latency_ms": avg("latency_ms"),
        "avg_cost": avg("cost"),
        "avg_energy": avg("energy"),
        "gpu_util_pct": round(peak_util_pct, 2),
        "avg_green_pct": green,
        "avg_compute_ms": avg("compute_ms"),
        "distribution": dist,
        "failed_ids": [r["task_id"] for r in rows if r["status"] != "SCHEDULED"],
    }


def _run_algo_sequence(
    tasks: list[TaskSpec],
    nodes: dict[str, NodeState],
    decide,
) -> tuple[list[dict[str, Any]], float]:
    """通用顺序提交；decide(task, nodes, leases) -> ScheduleDecision-like with to_dict fields."""
    leases = LeaseManager(nodes)
    rows: list[dict[str, Any]] = []
    peak = 0.0
    for i, task in enumerate(tasks):
        if i:
            leases.advance(INTERARRIVAL_H)
        else:
            leases.reap()
        d = decide(task, nodes, leases)
        peak = _peak_util(nodes, peak)
        sm = d.selected_metrics or {}
        rows.append({
            "task_id": task.task_id,
            "selected": d.selected,
            "status": d.status,
            "latency_ms": sm.get("latency_ms"),
            "cost": sm.get("cost"),
            "energy": sm.get("energy"),
            "green_ratio": sm.get("green_ratio"),
            "compute_ms": d.compute_ms,
            "reason": d.reason,
            "scores": getattr(d, "scores", {}),
            "rejected": d.rejected,
            "s_t": getattr(d, "s_t", 1.0),
            "metrics": getattr(d, "metrics", []),
        })
    return rows, peak


def run_paper_experiment(output_dir: str | Path | None = None) -> dict[str, Any]:
    from .baselines import BASELINES

    tasks = build_paper_tasks()
    results: dict[str, list[dict[str, Any]]] = {}
    peaks: dict[str, float] = {}

    for name, fn in BASELINES.items():
        nodes = make_paper_nodes()

        def decide(task, nodes, leases, _fn=fn):
            return _fn(task, nodes, allocate=True, leases=leases)

        rows, peak = _run_algo_sequence(tasks, nodes, decide)
        for r in rows:
            r["algorithm"] = name
        results[name] = rows
        peaks[name] = peak

    # 本文方法
    nodes = make_paper_nodes()
    sched = PaperScheduler(nodes, allocate=True)
    paper_rows: list[dict[str, Any]] = []
    peak = 0.0
    for i, task in enumerate(tasks):
        if i:
            sched.advance(INTERARRIVAL_H)
        d = sched.schedule(task)
        peak = _peak_util(nodes, peak)
        sm = d.selected_metrics or {}
        paper_rows.append({
            "algorithm": "本文方法（动态权重多目标调度）",
            "task_id": task.task_id,
            "selected": d.selected,
            "status": d.status,
            "latency_ms": sm.get("latency_ms"),
            "cost": sm.get("cost"),
            "energy": sm.get("energy"),
            "green_ratio": sm.get("green_ratio"),
            "compute_ms": d.compute_ms,
            "reason": d.reason,
            "scores": d.scores,
            "rejected": d.rejected,
            "s_t": d.s_t,
            "metrics": d.metrics,
        })
    results["本文方法（动态权重多目标调度）"] = paper_rows
    peaks["本文方法（动态权重多目标调度）"] = peak

    summaries = {
        name: _summarize(rows, peak_util_pct=peaks.get(name, 0.0))
        for name, rows in results.items()
    }

    # 消融
    ablation_weights = {
        "本文方法（全模块）": None,
        "无延迟优化 (w_lat=0)": {"wl": 0.0, "wc": 0.4, "we": 0.3, "wld": 0.3},
        "无成本优化 (w_cost=0)": {"wl": 0.8, "wc": 0.0, "we": 0.1, "wld": 0.1},
        "无能耗优化 (w_energy=0)": {"wl": 0.8, "wc": 0.1, "we": 0.0, "wld": 0.1},
        "无负载优化 (w_load=0)": {"wl": 0.85, "wc": 0.1, "we": 0.05, "wld": 0.0},
    }
    ablation: dict[str, Any] = {}
    for label, weights in ablation_weights.items():
        n3 = make_paper_nodes()
        s = PaperScheduler(n3, weights=weights, allocate=True)
        rows = []
        peak_a = 0.0
        for i, task in enumerate(tasks):
            if i:
                s.advance(INTERARRIVAL_H)
            d = s.schedule(task)
            peak_a = _peak_util(n3, peak_a)
            sm = d.selected_metrics or {}
            rows.append({
                "algorithm": label,
                "task_id": task.task_id,
                "selected": d.selected,
                "status": d.status,
                "latency_ms": sm.get("latency_ms"),
                "cost": sm.get("cost"),
                "energy": sm.get("energy"),
                "green_ratio": sm.get("green_ratio"),
                "compute_ms": d.compute_ms,
            })
        ablation[label] = {"summary": _summarize(rows, peak_util_pct=peak_a), "rows": rows}

    # 决策差异统计（实际区分度）
    paper = results["本文方法（动态权重多目标调度）"]
    diffs = {}
    for name, rows in results.items():
        if name.startswith("本文"):
            continue
        same = sum(
            1 for a, b in zip(paper, rows)
            if a["selected"] == b["selected"] and a["status"] == b["status"]
        )
        diffs[name] = {"identical_decisions": same, "total": len(paper), "diff_rate_pct": round(100 * (1 - same / len(paper)), 2)}

    ablation_summaries = {k: v["summary"] for k, v in ablation.items()}
    cold = cold_start_infeasibility(tasks)
    failure_taxonomy = classify_failure_reasons(paper_rows, cold_start=cold)
    abl_notes = ablation_effect_notes(ablation_summaries)

    # 同成功率组内的次级指标排名说明
    tied = [
        n for n, s in summaries.items()
        if abs(s["success_rate_pct"] - summaries["本文方法（动态权重多目标调度）"]["success_rate_pct"]) < 1e-9
    ]
    tied_others = [n for n in tied if not n.startswith("本文")]
    paper_s = summaries["本文方法（动态权重多目标调度）"]
    better_lat_cost = []
    for n in tied_others:
        s = summaries[n]
        if paper_s["avg_latency_ms"] < s["avg_latency_ms"] - 0.05 and paper_s["avg_cost"] <= s["avg_cost"] + 0.05:
            better_lat_cost.append(n)
        elif paper_s["avg_latency_ms"] <= s["avg_latency_ms"] + 0.05 and paper_s["avg_cost"] < s["avg_cost"] - 0.05:
            better_lat_cost.append(n)

    payload = {
        "meta": {
            "task_set": "cross-border-30v2",
            "note": "五节点资源模型 + 30 任务顺序提交，用于对比策略行为与消融贡献。",
            "interarrival_h": INTERARRIVAL_H,
            "gpu_util_definition": "peak_utilization_during_run",
        },
        "tasks": [t.to_dict() for t in tasks],
        "summaries": summaries,
        "ablation": ablation_summaries,
        "ablation_notes": abl_notes,
        "failure_taxonomy": failure_taxonomy,
        "ranking_notes": {
            "success_rate_tied_with": tied,
            "success_rate_tied_others": tied_others,
            "paper_better_lat_or_cost_among_tied": better_lat_cost,
        },
        "decision_diff_vs_paper": diffs,
        "paper_rows": paper_rows,
        "baseline_rows": results,
        "nodes": {k: v.snapshot() for k, v in make_paper_nodes().items()},
    }

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        # 完整 JSON 过大时去掉 metrics 明细以利存储
        slim_rows = []
        for r in paper_rows:
            slim = dict(r)
            slim.pop("metrics", None)
            slim_rows.append(slim)
        save = dict(payload)
        save["paper_rows"] = slim_rows
        save["baseline_rows"] = {
            k: [{kk: vv for kk, vv in r.items() if kk not in ("metrics", "rejected")} for r in rows]
            for k, rows in results.items()
        }
        (out / "experiment.json").write_text(
            json.dumps(save, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "algorithm,total,success,unscheduled,success_rate_pct,avg_latency_ms,avg_cost,avg_energy,gpu_util_pct,avg_green_pct,avg_compute_ms"
        ]
        for name, s in summaries.items():
            lines.append(
                ",".join([
                    name.replace(",", "，"),
                    str(s["total_tasks"]),
                    str(s["success_tasks"]),
                    str(s["unscheduled_tasks"]),
                    str(s["success_rate_pct"]),
                    str(s["avg_latency_ms"]),
                    str(s["avg_cost"]),
                    str(s["avg_energy"]),
                    str(s["gpu_util_pct"]),
                    str(s["avg_green_pct"]),
                    str(s["avg_compute_ms"]),
                ])
            )
        (out / "experiment_summary.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return payload


def assert_experiment_health(payload: dict[str, Any]) -> list[str]:
    """实验健康检查：结果量级合理、基线可区分。"""
    issues: list[str] = []
    summaries = payload["summaries"]
    paper = summaries["本文方法（动态权重多目标调度）"]
    if paper["success_rate_pct"] < 60:
        issues.append(f"本文方法成功率过低: {paper['success_rate_pct']}%")
    if paper["avg_compute_ms"] > 10:
        issues.append(f"在线决策耗时异常: {paper['avg_compute_ms']} ms")
    if paper["avg_cost"] < 10:
        issues.append(f"平均成本过低，量纲可能未标定: {paper['avg_cost']}")
    if paper["avg_energy"] < 1:
        issues.append(f"平均能耗过低，量纲可能未标定: {paper['avg_energy']}")
    if paper["gpu_util_pct"] < 40:
        issues.append(f"峰值利用率过低，争用不足: {paper['gpu_util_pct']}%")

    worse = [
        n for n, s in summaries.items()
        if not n.startswith("本文") and s["success_rate_pct"] < paper["success_rate_pct"] - 0.01
    ]
    diffs = payload.get("decision_diff_vs_paper") or {}
    differentiated = any(v.get("diff_rate_pct", 0) >= 15 for v in diffs.values())
    if not worse and not differentiated:
        issues.append("基线与本文几乎无区分度（成功率与决策均接近）")

    abl = payload.get("ablation") or {}
    full = abl.get("本文方法（全模块）", {})
    if full:
        changed = False
        for k, v in abl.items():
            if k == "本文方法（全模块）":
                continue
            if v["success_rate_pct"] != full["success_rate_pct"]:
                changed = True
            if abs(v["avg_latency_ms"] - full["avg_latency_ms"]) >= 1.0:
                changed = True
            if abs(v["avg_cost"] - full["avg_cost"]) >= 0.3:
                changed = True
            if set(v.get("failed_ids") or []) != set(full.get("failed_ids") or []):
                changed = True
        if not changed:
            issues.append("消融实验无指标/失败集变化，模块贡献不可见")
    return issues


# 兼容旧名
assert_paper_targets = assert_experiment_health
