"""将控制面真实/仿真节点映射为调度模型。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
# 兼容：src/controller（上级为 src）与线上扁平目录（同级有 scheduler/）
for candidate in (ROOT, ROOT.parent):
    if (candidate / "scheduler").is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scheduler.baselines import (  # noqa: E402
    baseline_fcfs,
    baseline_min_cost,
    baseline_min_latency,
    baseline_static_local,
)
from scheduler.model import (  # noqa: E402
    DEFAULT_WEIGHTS,
    LinkSpec,
    NodeState,
    TaskSpec,
    make_paper_nodes,
)
from scheduler.adaptive_scheduler import PaperScheduler, schedule_decision  # noqa: E402

REAL_REGIONS = ("海南", "重庆")


def node_dict_to_state(region: str, raw: dict[str, Any], *, paper_fallback: NodeState | None = None) -> NodeState:
    """把控制面 node snapshot 转为 NodeState。"""
    base = paper_fallback or make_paper_nodes().get(region)
    gpus = list(raw.get("gpus") or [])
    free_cards = sum(1 for g in gpus if float(g.get("free_gb", 0)) >= 1.0 and not g.get("busy"))
    if not gpus and raw.get("simulated"):
        gpu_free = int(raw.get("gpu_free", base.gpu_free if base else 0))
        gpu_cap = int(raw.get("gpu_capacity", base.gpu_capacity if base else gpu_free))
    else:
        gpu_cap = max(len(gpus), int(raw.get("gpu_capacity") or (base.gpu_capacity if base else 0)), 1)
        # 以“可承载下一任务的空闲卡数”近似 gpu_free
        gpu_free = free_cards if gpus else int(raw.get("gpu_free") or 0)

    links = dict(base.links) if base else {
        "china": LinkSpec(400, 0.5, float(raw.get("rtt_ms") or 30)),
        "sea": LinkSpec(200, 1.0, float(raw.get("rtt_ms") or 40) + 20),
        "central_asia": LinkSpec(150, 1.5, float(raw.get("rtt_ms") or 50) + 40),
    }
    # 用实测 RTT 覆盖 china 区链路
    if raw.get("rtt_ms") is not None and "china" in links:
        links["china"] = LinkSpec(
            bandwidth_mbps=links["china"].bandwidth_mbps,
            trans_cost_per_gb=links["china"].trans_cost_per_gb,
            rtt_ms=float(raw["rtt_ms"]),
        )

    return NodeState(
        name=region,
        gpu_capacity=gpu_cap,
        gpu_free=gpu_free,
        gpu_cost=float(raw.get("cost") or raw.get("gpu_cost") or (base.gpu_cost if base else 2.0)),
        green_ratio=float(raw.get("green_factor") or raw.get("green_ratio") or (base.green_ratio if base else 0.6)),
        has_tee=bool(raw.get("has_tee", base.has_tee if base else False)),
        region_tag=str(raw.get("region_tag") or (base.region_tag if base else "china")),
        power_kw=float(raw.get("power_kw") or (base.power_kw if base else 0.35)),
        links=links,
        healthy=bool(raw.get("healthy", True)),
        link_up=bool(raw.get("link_up", True)),
        reachable=bool(raw.get("reachable", True)),
        free_gb=float(raw.get("free_gb") or 0),
        gpus=gpus,
        model=str(raw.get("model") or (base.model if base else "")),
        agent_url=str(raw.get("agent_url") or ""),
        last_error=str(raw.get("last_error") or ""),
        simulated=bool(raw.get("simulated", region not in REAL_REGIONS)),
    )


def build_task_for_shard(
    *,
    memory_gb: float,
    affinity: str | None = None,
    source_zone: str = "china",
    require_tee: bool = False,
    latency_limit_ms: float = 200.0,
    budget: float = 80.0,
    latency_sensitivity: float = 1.2,
    data_gb: float = 1.0,
    runtime_h: float = 0.4,
    gpu_required: int = 1,
    task_id: str = "shard",
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        gpu_required=gpu_required,
        data_gb=data_gb,
        runtime_h=runtime_h,
        latency_limit_ms=latency_limit_ms,
        budget=budget,
        require_tee=require_tee,
        source_zone=source_zone,
        latency_sensitivity=latency_sensitivity,
        memory_gb=memory_gb,
        affinity=affinity,
        local_prefer=affinity,
    )


def choose_with_paper(
    nodes_raw: dict[str, dict[str, Any]],
    *,
    memory_gb: float,
    mode: str,
    affinity: str | None = None,
    task_overrides: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """统一入口：按策略选择节点，默认动态权重多目标。"""
    paper = make_paper_nodes()
    states = {
        name: node_dict_to_state(name, raw, paper_fallback=paper.get(name))
        for name, raw in nodes_raw.items()
    }
    # 真实执行仅可派发到有 agent 的节点
    executable = {k: v for k, v in states.items() if k in REAL_REGIONS or raw_has_agent(nodes_raw.get(k, {}))}
    pool = executable if executable else states

    ov = task_overrides or {}
    task = build_task_for_shard(
        memory_gb=memory_gb,
        affinity=affinity,
        source_zone=str(ov.get("source_zone") or "china"),
        require_tee=bool(ov.get("require_tee", False)),
        latency_limit_ms=float(ov.get("latency_limit_ms") or 250),
        budget=float(ov.get("budget") or 120),
        latency_sensitivity=float(ov.get("latency_sensitivity") or 1.2),
        data_gb=float(ov.get("data_gb") or 1.0),
        runtime_h=float(ov.get("runtime_h") or 0.35),
        gpu_required=int(ov.get("gpu_required") or 1),
        task_id=str(ov.get("task_id") or "live"),
    )

    if mode in ("动态权重多目标", "加权平均", "本文方法", ""):
        decision = schedule_decision(task, pool, check_memory=True, allocate=False)
    elif mode == "最小延迟":
        decision = baseline_min_latency(task, pool, allocate=False)
    elif mode == "最小成本":
        decision = baseline_min_cost(task, pool, allocate=False)
    elif mode == "静态本地":
        decision = baseline_static_local(task, pool, allocate=False)
    elif mode == "先到先服务":
        decision = baseline_fcfs(task, pool, allocate=False)
    elif mode == "海南优先":
        # 兼容旧 PoC：硬约束后海南优先，否则回退动态权重算法
        decision = schedule_decision(task, pool, check_memory=True, allocate=False)
        if decision.selected != "海南" and "海南" in decision.accepted:
            # 强制海南
            from scheduler.adaptive_scheduler import hard_filter

            accepted, rejected, metrics = hard_filter(task, pool, check_memory=True)
            if "海南" in accepted:
                m = next(x for x in metrics if x.node == "海南")
                decision.selected = "海南"
                decision.reason = "硬约束满足后命中海南优先规则"
                decision.selected_metrics = {
                    "latency_ms": round(m.latency_ms, 3),
                    "cost": round(m.cost, 4),
                    "energy": round(m.energy, 4),
                    "load": round(m.load, 4),
                    "green_ratio": pool["海南"].green_ratio,
                }
                decision.status = "SCHEDULED"
    else:
        decision = schedule_decision(task, pool, check_memory=True, allocate=False)

    region = decision.selected
    gpu_id = None
    gpu_index = None
    if region and pool[region].gpus:
        mem = memory_gb
        gpu = next((g for g in pool[region].gpus if float(g.get("free_gb", 0)) >= mem and not g.get("busy")), None)
        if gpu is None:
            gpu = next((g for g in pool[region].gpus if float(g.get("free_gb", 0)) >= mem), None)
        if gpu:
            gpu_id = gpu.get("id")
            gpu_index = gpu.get("index")

    payload = decision.to_dict()
    payload.update({
        "selected_region": region,
        "gpu_id": gpu_id,
        "gpu_index": gpu_index,
        "mode": mode or "动态权重多目标",
        "weights": dict(DEFAULT_WEIGHTS),
        "algorithm": "adaptive-dynamic-weight",
    })
    return region, payload


def raw_has_agent(raw: dict[str, Any]) -> bool:
    return bool(raw.get("agent_url")) and not raw.get("simulated", False)


def paper_fabric_nodes() -> dict[str, dict[str, Any]]:
    """五节点仿真资源池快照。"""
    return {name: node.snapshot() for name, node in make_paper_nodes().items()}
