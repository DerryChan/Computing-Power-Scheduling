"""跨境算力调度系统模型：节点、链路、任务与默认权重。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


# 默认权重：wl=0.733, wc=0.1, we=0.1, wld=0.1
DEFAULT_WEIGHTS = {
    "wl": 0.733,
    "wc": 0.100,
    "we": 0.100,
    "wld": 0.100,
}


@dataclass
class LinkSpec:
    """节点相对任务源区的链路参数。"""

    bandwidth_mbps: float
    trans_cost_per_gb: float
    rtt_ms: float


@dataclass
class NodeState:
    """智算节点运行时状态。"""

    name: str
    gpu_capacity: int
    gpu_free: int
    gpu_cost: float  # 元/卡·小时
    green_ratio: float
    has_tee: bool
    region_tag: str  # china / sea / central_asia / offshore
    power_kw: float = 0.35
    links: dict[str, LinkSpec] = field(default_factory=dict)
    healthy: bool = True
    link_up: bool = True
    reachable: bool = True
    # 真实节点扩展字段
    free_gb: float = 0.0
    gpus: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    agent_url: str = ""
    last_error: str = ""
    simulated: bool = True

    @property
    def load(self) -> float:
        if self.gpu_capacity <= 0:
            return 1.0
        used = max(0, self.gpu_capacity - self.gpu_free)
        return min(1.0, used / self.gpu_capacity)

    def snapshot(self) -> dict[str, Any]:
        return {
            "region": self.name,
            "name": self.name,
            "gpu_capacity": self.gpu_capacity,
            "gpu_free": self.gpu_free,
            "gpu_cost": self.gpu_cost,
            "cost": self.gpu_cost,
            "green_ratio": self.green_ratio,
            "green_factor": self.green_ratio,
            "has_tee": self.has_tee,
            "region_tag": self.region_tag,
            "load": round(self.load, 4),
            "healthy": self.healthy,
            "link_up": self.link_up,
            "reachable": self.reachable,
            "free_gb": self.free_gb,
            "gpus": list(self.gpus),
            "model": self.model,
            "agent_url": self.agent_url,
            "last_error": self.last_error,
            "simulated": self.simulated,
            "rtt_ms": self.links.get("china", LinkSpec(100, 0.5, 30)).rtt_ms,
        }

    def clone(self) -> "NodeState":
        return replace(self, links=dict(self.links), gpus=[dict(g) for g in self.gpus])


@dataclass
class TaskSpec:
    """待调度任务。"""

    task_id: str
    gpu_required: int
    data_gb: float
    runtime_h: float
    latency_limit_ms: float
    budget: float
    require_tee: bool = False
    source_zone: str = "china"  # china / sea / central_asia
    latency_sensitivity: float = 1.0  # S(t) 基线，越大越偏向低时延
    memory_gb: float = 8.0
    affinity: str | None = None
    local_prefer: str | None = None  # 静态本地路由偏好

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "gpu_required": self.gpu_required,
            "data_gb": self.data_gb,
            "runtime_h": self.runtime_h,
            "latency_limit_ms": self.latency_limit_ms,
            "budget": self.budget,
            "require_tee": self.require_tee,
            "source_zone": self.source_zone,
            "latency_sensitivity": self.latency_sensitivity,
            "memory_gb": self.memory_gb,
            "affinity": self.affinity,
            "local_prefer": self.local_prefer,
        }


def _link(bw: float, cost: float, rtt: float) -> LinkSpec:
    return LinkSpec(bandwidth_mbps=bw, trans_cost_per_gb=cost, rtt_ms=rtt)


def make_paper_nodes() -> dict[str, NodeState]:
    """五节点资源池：重庆 / 海南 / 香港 / 新加坡 / 新疆。"""
    # RTT 为区域间默认时延；带宽与传输单价按跨境专线合理默认（国内高带宽低单价）。
    nodes = {
        "重庆": NodeState(
            name="重庆",
            gpu_capacity=10,
            gpu_free=10,
            gpu_cost=2.0,
            green_ratio=0.55,
            has_tee=False,
            region_tag="china",
            power_kw=0.32,
            links={
                "china": _link(1000, 0.8, 20),
                "sea": _link(200, 3.5, 50),
                "central_asia": _link(150, 4.0, 80),
            },
            model="仿真·异构 GPU",
        ),
        "海南": NodeState(
            name="海南",
            gpu_capacity=6,
            gpu_free=6,
            gpu_cost=2.5,
            green_ratio=0.70,
            has_tee=False,
            region_tag="china",
            power_kw=0.35,
            links={
                "china": _link(800, 1.0, 15),
                "sea": _link(400, 2.8, 20),
                "central_asia": _link(120, 4.5, 100),
            },
            model="仿真·RTX 类",
        ),
        "香港": NodeState(
            name="香港",
            gpu_capacity=8,
            gpu_free=8,
            gpu_cost=3.0,
            green_ratio=0.60,
            has_tee=True,
            region_tag="offshore",
            power_kw=0.38,
            links={
                "china": _link(600, 2.2, 15),
                "sea": _link(500, 2.0, 30),
                "central_asia": _link(180, 4.2, 90),
            },
            model="仿真·离岸 TEE",
        ),
        "新加坡": NodeState(
            name="新加坡",
            gpu_capacity=12,
            gpu_free=12,
            gpu_cost=3.5,
            green_ratio=0.65,
            has_tee=True,
            region_tag="sea",
            power_kw=0.40,
            links={
                "china": _link(300, 3.2, 25),
                "sea": _link(1000, 1.0, 10),
                "central_asia": _link(100, 5.0, 120),
            },
            model="仿真·离岸 TEE",
        ),
        "新疆": NodeState(
            name="新疆",
            gpu_capacity=4,
            gpu_free=4,
            gpu_cost=1.5,
            green_ratio=0.85,
            has_tee=False,
            region_tag="central_asia",
            power_kw=0.30,
            links={
                "china": _link(400, 1.5, 30),
                "sea": _link(120, 4.8, 80),
                "central_asia": _link(600, 1.2, 60),
            },
            model="仿真·绿电节点",
        ),
    }
    return nodes
