"""调度核心：硬约束过滤 + 最小-最大规范化 + 自适应动态权重打分。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .model import DEFAULT_WEIGHTS, LinkSpec, NodeState, TaskSpec


EPS = 1e-9


class LeaseManager:
    """按仿真时钟释放 GPU 占用，支持在线顺序提交。"""

    def __init__(self, nodes: dict[str, NodeState]) -> None:
        self.nodes = nodes
        self.now_h = 0.0
        self._leases: list[tuple[float, str, int]] = []

    def advance(self, dt_h: float) -> None:
        self.now_h += max(0.0, dt_h)
        self.reap()

    def reap(self) -> None:
        remain: list[tuple[float, str, int]] = []
        for release_at, node_name, gpus in self._leases:
            if release_at <= self.now_h + EPS:
                node = self.nodes.get(node_name)
                if node:
                    node.gpu_free = min(node.gpu_capacity, node.gpu_free + gpus)
            else:
                remain.append((release_at, node_name, gpus))
        self._leases = remain

    def allocate(self, node_name: str, gpus: int, runtime_h: float) -> None:
        node = self.nodes[node_name]
        node.gpu_free = max(0, node.gpu_free - gpus)
        self._leases.append((self.now_h + max(runtime_h, 0.01), node_name, gpus))


@dataclass
class CandidateMetrics:
    node: str
    latency_ms: float
    cost: float
    energy: float
    load: float
    n_latency: float = 0.0
    n_cost: float = 0.0
    n_energy: float = 0.0
    score: float = 0.0
    s_t: float = 1.0
    link: LinkSpec | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduleDecision:
    selected: str | None
    status: str
    reason: str
    accepted: list[str]
    rejected: list[dict[str, str]]
    metrics: list[dict[str, Any]]
    scores: dict[str, float]
    compute_ms: float
    selected_metrics: dict[str, Any] | None = None
    weights: dict[str, float] = field(default_factory=dict)
    s_t: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_region": self.selected,
            "selected": self.selected,
            "status": self.status,
            "reason": self.reason,
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
            "metrics": list(self.metrics),
            "scores": dict(self.scores),
            "compute_ms": self.compute_ms,
            "selected_metrics": self.selected_metrics,
            "weights": dict(self.weights),
            "s_t": self.s_t,
        }


def resolve_link(node: NodeState, source_zone: str) -> LinkSpec:
    link = node.links.get(source_zone)
    if link is not None:
        return link
    # 真实两节点兜底：用 china 区或构造伪链路
    if node.links:
        return next(iter(node.links.values()))
    rtt = 30.0
    if node.gpus or not node.simulated:
        # 控制面注入的 rtt 可走 snapshot；这里用保守默认
        rtt = 40.0
    return LinkSpec(bandwidth_mbps=200.0, trans_cost_per_gb=1.0, rtt_ms=rtt)


def compute_latency_ms(task: TaskSpec, node: NodeState, link: LinkSpec | None = None) -> float:
    """Latency_i = RTT_i + 传输项 + 处理项。

    传输项采用 (D_j*8)/(B_ij/100)，与 RTT 同为毫秒量纲的抽象模型。
    处理项按卡数与运行时长给出可复现的代理时延（非真实 wall-clock 推理时延）。
    """
    link = link or resolve_link(node, task.source_zone)
    transfer_ms = (task.data_gb * 8.0) / max(link.bandwidth_mbps / 100.0, EPS)
    runtime_term = 4.0 + 3.0 * task.gpu_required + 4.0 * min(task.runtime_h, 6.0)
    return link.rtt_ms + transfer_ms + runtime_term


def compute_cost(task: TaskSpec, node: NodeState, link: LinkSpec | None = None) -> float:
    """Cost_i = GPU_Required * GPU_Cost * Runtime + D_j * C_trans（元）。"""
    link = link or resolve_link(node, task.source_zone)
    compute = task.gpu_required * node.gpu_cost * task.runtime_h
    transfer = task.data_gb * link.trans_cost_per_gb
    return compute + transfer


def compute_energy(task: TaskSpec, node: NodeState) -> float:
    """能耗/碳排代理：卡数 × 功率 × 时长 × (1-绿电) × 强度系数。

    强度系数 8.0 把结果映射到与租用时长可比的“碳排指数”，便于多目标归一化。
    """
    carbon_factor = max(0.0, 1.0 - node.green_ratio)
    return task.gpu_required * node.power_kw * task.runtime_h * 8.0 * carbon_factor


def adaptive_sensitivity(task: TaskSpec, min_latency_ms: float) -> float:
    """结合任务时延敏感度与 SLA 松紧度自适应调节 S(t)。"""
    base = max(0.2, float(task.latency_sensitivity))
    # SLA 越紧（limit 越接近理论最小时延），S(t) 越大，强化时延项
    ratio = min_latency_ms / max(task.latency_limit_ms, EPS)
    tightness = max(0.0, min(1.5, ratio))
    return max(0.5, min(2.5, base * (0.75 + 0.75 * tightness)))


def min_max_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < EPS:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def hard_filter(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    *,
    check_memory: bool = False,
) -> tuple[list[str], list[dict[str, str]], list[CandidateMetrics]]:
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    metrics: list[CandidateMetrics] = []

    for name, node in nodes.items():
        if task.affinity and name != task.affinity:
            rejected.append({"region": name, "reason": "亲和性不匹配"})
            continue
        if not node.reachable:
            rejected.append({"region": name, "reason": f"节点不可达: {node.last_error or 'unreachable'}"})
            continue
        if not node.healthy:
            rejected.append({"region": name, "reason": "节点不健康"})
            continue
        if not node.link_up:
            rejected.append({"region": name, "reason": "链路不可用"})
            continue
        if task.require_tee and not node.has_tee:
            rejected.append({"region": name, "reason": "TEE 合规硬护栏：节点未配置可信执行环境"})
            continue
        if node.gpu_free < task.gpu_required:
            rejected.append({"region": name, "reason": f"GPU 剩余容量不足（需{task.gpu_required}，剩{node.gpu_free}）"})
            continue
        if check_memory and task.memory_gb > 0:
            ok = any(float(g.get("free_gb", 0)) >= task.memory_gb for g in (node.gpus or []))
            if node.gpus and not ok:
                rejected.append({"region": name, "reason": "单卡可用显存不足"})
                continue
            if not node.gpus and node.free_gb < task.memory_gb and not node.simulated:
                rejected.append({"region": name, "reason": "单卡可用显存不足"})
                continue

        link = resolve_link(node, task.source_zone)
        latency = compute_latency_ms(task, node, link)
        cost = compute_cost(task, node, link)
        energy = compute_energy(task, node)

        if latency > task.latency_limit_ms + EPS:
            rejected.append({"region": name, "reason": f"时延超限（{latency:.1f}>{task.latency_limit_ms:.1f} ms）"})
            continue
        if cost > task.budget + EPS:
            rejected.append({"region": name, "reason": f"预算超限（{cost:.2f}>{task.budget:.2f} 元）"})
            continue

        cm = CandidateMetrics(
            node=name,
            latency_ms=latency,
            cost=cost,
            energy=energy,
            load=node.load,
            link=link,
            detail={
                "rtt_ms": link.rtt_ms,
                "bandwidth_mbps": link.bandwidth_mbps,
                "trans_cost": task.data_gb * link.trans_cost_per_gb,
                "compute_cost": task.gpu_required * node.gpu_cost * task.runtime_h,
                "green_ratio": node.green_ratio,
                "gpu_free": node.gpu_free,
                "gpu_capacity": node.gpu_capacity,
            },
        )
        accepted.append(name)
        metrics.append(cm)

    return accepted, rejected, metrics


def score_candidates(
    task: TaskSpec,
    metrics: list[CandidateMetrics],
    weights: dict[str, float] | None = None,
) -> tuple[list[CandidateMetrics], float]:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    if not metrics:
        return [], 1.0

    min_lat = min(m.latency_ms for m in metrics)
    s_t = adaptive_sensitivity(task, min_lat)

    n_lat = min_max_normalize([m.latency_ms for m in metrics])
    n_cost = min_max_normalize([m.cost for m in metrics])
    n_energy = min_max_normalize([m.energy for m in metrics])

    wl, wc, we, wld = weights["wl"], weights["wc"], weights["we"], weights["wld"]
    for i, m in enumerate(metrics):
        m.n_latency = n_lat[i]
        m.n_cost = n_cost[i]
        m.n_energy = n_energy[i]
        m.s_t = s_t
        # Score_i = wl * S(t) * N_lat + wc * N_cost + we * N_energy + wld * Load
        m.score = (
            wl * s_t * m.n_latency
            + wc * m.n_cost
            + we * m.n_energy
            + wld * m.load
        )
    return metrics, s_t


class PaperScheduler:
    """在线动态权重多目标调度器。"""

    def __init__(
        self,
        nodes: dict[str, NodeState] | None = None,
        weights: dict[str, float] | None = None,
        *,
        check_memory: bool = False,
        allocate: bool = True,
    ) -> None:
        self.nodes = nodes or {}
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.check_memory = check_memory
        self.allocate = allocate
        self.decisions: list[dict[str, Any]] = []
        self.leases = LeaseManager(self.nodes)

    def set_nodes(self, nodes: dict[str, NodeState]) -> None:
        self.nodes = nodes
        self.leases = LeaseManager(self.nodes)

    def advance(self, dt_h: float) -> None:
        self.leases.advance(dt_h)

    def schedule(self, task: TaskSpec) -> ScheduleDecision:
        self.leases.reap()
        t0 = time.perf_counter()
        accepted, rejected, metrics = hard_filter(
            task, self.nodes, check_memory=self.check_memory
        )
        if not metrics:
            decision = ScheduleDecision(
                selected=None,
                status="UNSCHEDULED",
                reason="无满足硬约束的候选节点",
                accepted=[],
                rejected=rejected,
                metrics=[],
                scores={},
                compute_ms=(time.perf_counter() - t0) * 1000.0,
                weights=dict(self.weights),
            )
            self.decisions.append(decision.to_dict())
            return decision

        metrics, s_t = score_candidates(task, metrics, self.weights)
        # 得分越小越优；同分时偏好绿电更高、成本更低
        metrics.sort(key=lambda m: (m.score, m.cost, -self.nodes[m.node].green_ratio, m.latency_ms))
        best = metrics[0]
        selected = best.node

        if self.allocate:
            self.leases.allocate(selected, task.gpu_required, task.runtime_h)

        compute_ms = (time.perf_counter() - t0) * 1000.0
        decision = ScheduleDecision(
            selected=selected,
            status="SCHEDULED",
            reason=(
                f"动态权重多目标最优：Score={best.score:.4f} "
                f"(S(t)={s_t:.3f}, lat={best.latency_ms:.1f}ms, cost={best.cost:.2f}, "
                f"energy={best.energy:.3f}, load={best.load:.2f})"
            ),
            accepted=accepted,
            rejected=rejected,
            metrics=[
                {
                    "node": m.node,
                    "latency_ms": round(m.latency_ms, 3),
                    "cost": round(m.cost, 4),
                    "energy": round(m.energy, 4),
                    "load": round(m.load, 4),
                    "n_latency": round(m.n_latency, 4),
                    "n_cost": round(m.n_cost, 4),
                    "n_energy": round(m.n_energy, 4),
                    "score": round(m.score, 6),
                    "detail": m.detail,
                }
                for m in metrics
            ],
            scores={m.node: round(m.score, 6) for m in metrics},
            compute_ms=compute_ms,
            selected_metrics={
                "latency_ms": round(best.latency_ms, 3),
                "cost": round(best.cost, 4),
                "energy": round(best.energy, 4),
                "load": round(best.load, 4),
                "score": round(best.score, 6),
                "green_ratio": self.nodes[selected].green_ratio,
            },
            weights=dict(self.weights),
            s_t=s_t,
        )
        self.decisions.append(decision.to_dict())
        return decision

    def release(self, node_name: str, gpu_required: int) -> None:
        node = self.nodes.get(node_name)
        if not node:
            return
        node.gpu_free = min(node.gpu_capacity, node.gpu_free + gpu_required)


def schedule_decision(
    task: TaskSpec,
    nodes: dict[str, NodeState],
    weights: dict[str, float] | None = None,
    *,
    check_memory: bool = False,
    allocate: bool = False,
) -> ScheduleDecision:
    sched = PaperScheduler(nodes, weights, check_memory=check_memory, allocate=allocate)
    return sched.schedule(task)
