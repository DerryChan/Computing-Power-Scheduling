#!/usr/bin/env python3
"""调度算法单元测试（不依赖外部服务）。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduler.experiment import (
    ablation_effect_notes,
    assert_experiment_health,
    build_paper_tasks,
    classify_failure_reasons,
    cold_start_infeasibility,
    run_paper_experiment,
)
from scheduler.model import DEFAULT_WEIGHTS, TaskSpec, make_paper_nodes
from scheduler.adaptive_scheduler import (
    adaptive_sensitivity,
    compute_cost,
    compute_energy,
    compute_latency_ms,
    hard_filter,
    min_max_normalize,
    schedule_decision,
    score_candidates,
)
from controller.scheduler_bridge import choose_with_paper


class TestNormalize(unittest.TestCase):
    def test_minmax(self):
        self.assertEqual(min_max_normalize([10, 20, 30]), [0.0, 0.5, 1.0])
        self.assertEqual(min_max_normalize([5, 5, 5]), [0.0, 0.0, 0.0])
        self.assertEqual(min_max_normalize([]), [])


class TestAdaptiveSensitivity(unittest.TestCase):
    def test_tighter_sla_raises_st(self):
        loose = TaskSpec("A", 1, 1, 1, latency_limit_ms=200, budget=100, latency_sensitivity=1.0)
        tight = TaskSpec("B", 1, 1, 1, latency_limit_ms=60, budget=100, latency_sensitivity=1.0)
        s_loose = adaptive_sensitivity(loose, min_latency_ms=50)
        s_tight = adaptive_sensitivity(tight, min_latency_ms=50)
        self.assertGreater(s_tight, s_loose)

    def test_bounds(self):
        t = TaskSpec("C", 1, 1, 1, latency_limit_ms=1, budget=100, latency_sensitivity=3.0)
        s = adaptive_sensitivity(t, min_latency_ms=100)
        self.assertGreaterEqual(s, 0.5)
        self.assertLessEqual(s, 2.5)


class TestHardConstraints(unittest.TestCase):
    def setUp(self):
        self.nodes = make_paper_nodes()

    def test_budget_conflict(self):
        task = TaskSpec(
            task_id="X", gpu_required=4, data_gb=18, runtime_h=5.0,
            latency_limit_ms=280, budget=39.61, source_zone="sea",
        )
        acc, rej, _ = hard_filter(task, self.nodes)
        self.assertEqual(acc, [])
        self.assertTrue(any("预算" in r["reason"] for r in rej))

    def test_tee_guard(self):
        task = TaskSpec(
            task_id="TEE", gpu_required=1, data_gb=1, runtime_h=1,
            latency_limit_ms=200, budget=100, require_tee=True, source_zone="china",
        )
        acc, rej, _ = hard_filter(task, self.nodes)
        self.assertTrue(all(self.nodes[n].has_tee for n in acc))
        self.assertTrue(any("TEE" in r["reason"] for r in rej))

    def test_cost_energy_positive(self):
        task = build_paper_tasks()[0]
        for node in self.nodes.values():
            self.assertGreater(compute_cost(task, node), 0)
            self.assertGreaterEqual(compute_energy(task, node), 0)
            self.assertGreater(compute_latency_ms(task, node), 0)


class TestScoring(unittest.TestCase):
    def test_picks_feasible_node(self):
        nodes = make_paper_nodes()
        task = TaskSpec(
            task_id="S", gpu_required=1, data_gb=2, runtime_h=1.0,
            latency_limit_ms=200, budget=80, source_zone="china", latency_sensitivity=1.2,
        )
        d = schedule_decision(task, nodes, allocate=False)
        self.assertEqual(d.status, "SCHEDULED")
        self.assertIn(d.selected, nodes)
        self.assertAlmostEqual(d.scores[d.selected], min(d.scores.values()), places=9)

    def test_score_formula_golden(self):
        """固定两候选：验证 Score = wl*S*Nlat + wc*Ncost + we*Nenergy + wld*Load。"""
        nodes = make_paper_nodes()
        # 只保留海南/重庆，便于可控
        pool = {"海南": nodes["海南"], "重庆": nodes["重庆"]}
        pool["海南"].gpu_free = pool["海南"].gpu_capacity
        pool["重庆"].gpu_free = pool["重庆"].gpu_capacity
        task = TaskSpec(
            task_id="G", gpu_required=1, data_gb=2, runtime_h=1.0,
            latency_limit_ms=300, budget=200, source_zone="china", latency_sensitivity=1.0,
        )
        acc, _, metrics = hard_filter(task, pool)
        self.assertEqual(set(acc), {"海南", "重庆"})
        scored, s_t = score_candidates(task, metrics, DEFAULT_WEIGHTS)
        by = {m.node: m for m in scored}
        for name, m in by.items():
            expected = (
                DEFAULT_WEIGHTS["wl"] * s_t * m.n_latency
                + DEFAULT_WEIGHTS["wc"] * m.n_cost
                + DEFAULT_WEIGHTS["we"] * m.n_energy
                + DEFAULT_WEIGHTS["wld"] * m.load
            )
            self.assertAlmostEqual(m.score, expected, places=9, msg=name)


class TestRealBridge(unittest.TestCase):
    def test_vram16_excludes_chongqing(self):
        nodes = {
            "海南": {
                "region": "海南", "rtt_ms": 15, "cost": 2.5, "green_factor": 0.7,
                "healthy": True, "link_up": True, "reachable": True, "agent_url": "http://x",
                "gpus": [{"id": "HN0", "index": 0, "free_gb": 22, "total_gb": 24, "busy": False}],
                "free_gb": 22, "simulated": False,
            },
            "重庆": {
                "region": "重庆", "rtt_ms": 20, "cost": 2.0, "green_factor": 0.55,
                "healthy": True, "link_up": True, "reachable": True, "agent_url": "http://y",
                "gpus": [{"id": f"CQ{i}", "index": i, "free_gb": 11, "total_gb": 12, "busy": False} for i in range(4)],
                "free_gb": 44, "simulated": False,
            },
        }
        region, decision = choose_with_paper(nodes, memory_gb=16, mode="动态权重多目标")
        self.assertEqual(region, "海南")
        self.assertTrue(any(r["region"] == "重庆" and "显存" in r["reason"] for r in decision["rejected"]))


class TestExperiment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_paper_experiment(None)

    def test_experiment_health(self):
        issues = assert_experiment_health(self.payload)
        self.assertEqual(issues, [], msg=issues)

    def test_peak_util_definition(self):
        self.assertEqual(self.payload["meta"]["gpu_util_definition"], "peak_utilization_during_run")
        paper = self.payload["summaries"]["本文方法（动态权重多目标调度）"]
        self.assertGreaterEqual(paper["gpu_util_pct"], 40)

    def test_failure_taxonomy_present(self):
        tax = self.payload["failure_taxonomy"]
        self.assertTrue(tax["by_task"])
        self.assertTrue(tax["counts"])
        cold = cold_start_infeasibility(build_paper_tasks())
        self.assertIn("T03", cold)
        self.assertIn("T15", cold)
        self.assertIn("T25", cold)

    def test_ablation_notes_mention_load_if_inert(self):
        notes = self.payload.get("ablation_notes") or ablation_effect_notes(self.payload["ablation"])
        load_notes = [n for n in notes if "无负载优化" in n]
        self.assertTrue(load_notes)
        full = self.payload["ablation"]["本文方法（全模块）"]
        noload = self.payload["ablation"]["无负载优化 (w_load=0)"]
        same = (
            full["success_rate_pct"] == noload["success_rate_pct"]
            and abs(full["avg_latency_ms"] - noload["avg_latency_ms"]) < 0.05
            and abs(full["avg_cost"] - noload["avg_cost"]) < 0.05
            and set(full["failed_ids"]) == set(noload["failed_ids"])
        )
        if same:
            self.assertTrue(any("未改变调度结果" in n or "一致" in n for n in load_notes))

    def test_ranking_notes_tie_aware(self):
        notes = self.payload.get("ranking_notes") or {}
        self.assertIn("success_rate_tied_with", notes)
        self.assertTrue(any("本文" in x for x in notes["success_rate_tied_with"]))
        others = notes.get("success_rate_tied_others") or []
        self.assertTrue(all(not x.startswith("本文") for x in others))

    def test_determinism_two_runs(self):
        a = run_paper_experiment(None)
        b = run_paper_experiment(None)
        self.assertEqual(
            a["summaries"]["本文方法（动态权重多目标调度）"]["failed_ids"],
            b["summaries"]["本文方法（动态权重多目标调度）"]["failed_ids"],
        )
        self.assertEqual(
            a["summaries"]["本文方法（动态权重多目标调度）"]["distribution"],
            b["summaries"]["本文方法（动态权重多目标调度）"]["distribution"],
        )
        self.assertEqual(
            a["summaries"]["遗传算法"]["failed_ids"],
            b["summaries"]["遗传算法"]["failed_ids"],
        )


class TestClassifyHelpers(unittest.TestCase):
    def test_classify_uses_cold_start(self):
        rows = [{
            "task_id": "T03", "status": "UNSCHEDULED",
            "rejected": [{"region": "重庆", "reason": "GPU 剩余容量不足（需4，剩0）"}],
        }]
        tax = classify_failure_reasons(rows, cold_start={"T03": "空集群即不可行：预算超限"})
        self.assertEqual(tax["by_task"]["T03"], "空集群预算硬冲突")


if __name__ == "__main__":
    unittest.main()
