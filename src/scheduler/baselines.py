"""对比基线：静态本地、FCFS、最小延迟、最小成本、可行性受限遗传算法。"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from .model import NodeState, TaskSpec
from .adaptive_scheduler import (
    CandidateMetrics,
    LeaseManager,
    PaperScheduler,
    ScheduleDecision,
    hard_filter,
)


def _decision_from_pick(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    accepted: list[str],
    rejected: list[dict[str, str]],
    metrics: list[CandidateMetrics],
    selected: str | None,
    reason: str,
    t0: float,
    *,
    allocate: bool,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    if selected is None:
        return ScheduleDecision(
            selected=None,
            status="UNSCHEDULED",
            reason=reason,
            accepted=accepted,
            rejected=rejected,
            metrics=[],
            scores={},
            compute_ms=(time.perf_counter() - t0) * 1000.0,
        )
    m = next(x for x in metrics if x.node == selected)
    if allocate:
        if leases is not None:
            leases.allocate(selected, task.gpu_required, task.runtime_h)
        else:
            nodes[selected].gpu_free = max(0, nodes[selected].gpu_free - task.gpu_required)
    return ScheduleDecision(
        selected=selected,
        status="SCHEDULED",
        reason=reason,
        accepted=accepted,
        rejected=rejected,
        metrics=[{
            "node": x.node,
            "latency_ms": round(x.latency_ms, 3),
            "cost": round(x.cost, 4),
            "energy": round(x.energy, 4),
            "load": round(x.load, 4),
            "score": 0.0,
        } for x in metrics],
        scores={selected: 0.0},
        compute_ms=(time.perf_counter() - t0) * 1000.0,
        selected_metrics={
            "latency_ms": round(m.latency_ms, 3),
            "cost": round(m.cost, 4),
            "energy": round(m.energy, 4),
            "load": round(m.load, 4),
            "green_ratio": nodes[selected].green_ratio,
        },
    )


def baseline_static_local(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    allocate: bool = True,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    if leases is not None:
        leases.reap()
    t0 = time.perf_counter()
    prefer = task.local_prefer or {
        "china": "重庆",
        "sea": "新加坡",
        "central_asia": "新疆",
    }.get(task.source_zone, "重庆")
    accepted, rejected, metrics = hard_filter(task, nodes)
    if prefer in accepted:
        return _decision_from_pick(
            task, nodes, accepted, rejected, metrics, prefer,
            f"静态本地路由固定映射至 {prefer}", t0, allocate=allocate, leases=leases,
        )
    return _decision_from_pick(
        task, nodes, accepted, rejected, metrics, None,
        f"静态本地节点 {prefer} 不满足硬约束", t0, allocate=False, leases=leases,
    )


def baseline_fcfs(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    allocate: bool = True,
    order: list[str] | None = None,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    if leases is not None:
        leases.reap()
    t0 = time.perf_counter()
    accepted, rejected, metrics = hard_filter(task, nodes)
    if not accepted:
        return _decision_from_pick(
            task, nodes, accepted, rejected, metrics, None,
            "无满足硬约束的候选节点", t0, allocate=False, leases=leases,
        )
    seq = order or list(nodes.keys())
    selected = next((n for n in seq if n in accepted), accepted[0])
    return _decision_from_pick(
        task, nodes, accepted, rejected, metrics, selected,
        f"先到先服务：按节点顺序选中 {selected}", t0, allocate=allocate, leases=leases,
    )


def baseline_min_latency(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    allocate: bool = True,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    if leases is not None:
        leases.reap()
    t0 = time.perf_counter()
    accepted, rejected, metrics = hard_filter(task, nodes)
    if not metrics:
        return _decision_from_pick(
            task, nodes, accepted, rejected, metrics, None,
            "无满足硬约束的候选节点", t0, allocate=False, leases=leases,
        )
    selected = min(metrics, key=lambda m: m.latency_ms).node
    return _decision_from_pick(
        task, nodes, accepted, rejected, metrics, selected,
        "最小延迟策略", t0, allocate=allocate, leases=leases,
    )


def baseline_min_cost(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    allocate: bool = True,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    if leases is not None:
        leases.reap()
    t0 = time.perf_counter()
    accepted, rejected, metrics = hard_filter(task, nodes)
    if not metrics:
        return _decision_from_pick(
            task, nodes, accepted, rejected, metrics, None,
            "无满足硬约束的候选节点", t0, allocate=False, leases=leases,
        )
    selected = min(metrics, key=lambda m: m.cost).node
    return _decision_from_pick(
        task, nodes, accepted, rejected, metrics, selected,
        "最小成本策略", t0, allocate=allocate, leases=leases,
    )


def baseline_ga(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    allocate: bool = True,
    population: int = 120,
    generations: int = 80,
    seed: int = 42,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    """可行性受限遗传算法强基线：仅在硬约束可行集上搜索加权最优。"""
    if leases is not None:
        leases.reap()
    t0 = time.perf_counter()
    accepted, rejected, metrics = hard_filter(task, nodes)
    if not metrics:
        return _decision_from_pick(
            task, nodes, accepted, rejected, metrics, None,
            "无满足硬约束的候选节点", t0, allocate=False, leases=leases,
        )

    rng = random.Random(seed + int("".join(ch for ch in task.task_id if ch.isdigit()) or "0"))
    names = [m.node for m in metrics]
    by_name = {m.node: m for m in metrics}

    def fitness(name: str) -> float:
        m = by_name[name]
        return (
            0.45 * m.latency_ms / 200.0
            + 0.25 * m.cost / 80.0
            + 0.15 * m.energy / 5.0
            + 0.15 * m.load
        )

    pop = [rng.choice(names) for _ in range(population)]
    best = min(pop, key=fitness)
    for _ in range(generations):
        scored = sorted(pop, key=fitness)
        elites = scored[: max(2, population // 5)]
        children: list[str] = list(elites)
        while len(children) < population:
            p1, p2 = rng.choice(elites), rng.choice(elites)
            child = p1 if fitness(p1) <= fitness(p2) else p2
            if rng.random() < 0.25:
                child = rng.choice(names)
            children.append(child)
        pop = children
        cand = min(pop, key=fitness)
        if fitness(cand) < fitness(best):
            best = cand

    return _decision_from_pick(
        task, nodes, accepted, rejected, metrics, best,
        "可行性受限遗传算法搜索最优可行节点", t0, allocate=allocate, leases=leases,
    )


BASELINES: dict[str, Callable[..., ScheduleDecision]] = {
    "静态本地": baseline_static_local,
    "先到先服务": baseline_fcfs,
    "最小延迟": baseline_min_latency,
    "最小成本": baseline_min_cost,
    "遗传算法": baseline_ga,
}


def run_baseline(
    name: str,
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    allocate: bool = True,
    leases: LeaseManager | None = None,
) -> ScheduleDecision:
    fn = BASELINES[name]
    return fn(task, nodes, allocate=allocate, leases=leases)


def run_sequence(
    tasks: list[TaskSpec],
    nodes: dict[str, NodeState],
    decide: Callable[[TaskSpec, dict[str, NodeState], LeaseManager], ScheduleDecision],
    *,
    interarrival_h: float = 0.12,
) -> list[dict[str, Any]]:
    leases = LeaseManager(nodes)
    rows: list[dict[str, Any]] = []
    for i, task in enumerate(tasks):
        if i:
            leases.advance(interarrival_h)
        else:
            leases.reap()
        d = decide(task, nodes, leases)
        rows.append(_row(task, d, getattr(decide, "algo_name", "algo")))
    return rows


def run_all_baselines(
    tasks: list[TaskSpec],
    nodes_factory,
    *,
    interarrival_h: float = 0.12,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, fn in BASELINES.items():
        nodes = nodes_factory()
        leases = LeaseManager(nodes)
        rows = []
        for i, task in enumerate(tasks):
            if i:
                leases.advance(interarrival_h)
            d = fn(task, nodes, allocate=True, leases=leases)
            rows.append(_row(task, d, name))
        out[name] = rows

    nodes = nodes_factory()
    sched = PaperScheduler(nodes, allocate=True)
    rows = []
    for i, task in enumerate(tasks):
        if i:
            sched.advance(interarrival_h)
        d = sched.schedule(task)
        rows.append(_row(task, d, "本文方法（动态权重多目标调度）"))
    out["本文方法（动态权重多目标调度）"] = rows
    return out


def _row(task: TaskSpec, d: ScheduleDecision, algo: str) -> dict[str, Any]:
    sm = d.selected_metrics or {}
    return {
        "algorithm": algo,
        "task_id": task.task_id,
        "selected": d.selected,
        "status": d.status,
        "latency_ms": sm.get("latency_ms"),
        "cost": sm.get("cost"),
        "energy": sm.get("energy"),
        "green_ratio": sm.get("green_ratio"),
        "compute_ms": d.compute_ms,
        "reason": d.reason,
        "rejected": d.rejected,
    }
