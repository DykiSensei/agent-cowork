"""会话级 token 硬护栏（§12 M9）。

补的缺口：`TaskSpec.token_budget` 管一个任务，`budget_escalation_ratio` 是软的
（越线只升级），于是一次复合运行 = N 个各自有上限的任务、总量没有上限。
此前唯一的硬强制来自 LiteLLM 的 virtual key —— 而那是**可选组件**。

设计上最要紧的两条，这里各有用例钉着：

1. **多个 Backend 共享同一个 CostGuard**。复核者、路由到别家的 Subagent、
   架构师是不同对象，但花的是同一笔钱；每个包一个自己的护栏等于没有护栏。
2. **超限抛的是 `BudgetExceeded`**，也就是 LiteLLM 硬拒绝时抛的那一个。
   它天然汇进既有链路，不需要新控制流 —— 这是「包一层」而不是「到处加检查」
   的全部理由。
"""

from __future__ import annotations

import threading
import unittest

from cowork.llm.budget import BudgetedBackend, CostGuard
from cowork.llm.errors import BudgetExceeded, ModelError
from cowork.llm.scripted import ScriptedBackend
from cowork.types import AgentContext, Criterion, TaskClass, TaskSpec


def _spec() -> TaskSpec:
    return TaskSpec(
        goal="g", acceptance=[Criterion("c1", "d")], task_class=TaskClass.TOOL_CALL,
    )


def _ctx(spec) -> AgentContext:
    return AgentContext(task_spec=spec)


class TestCostGuard(unittest.TestCase):
    def test_counts_up_and_blocks_at_the_limit(self):
        g = CostGuard(1000)
        g.check()          # 还没花钱
        g.spend(600)
        g.check()          # 600 < 1000，还能跑
        g.spend(500)
        with self.assertRaises(BudgetExceeded):
            g.check()

    def test_zero_means_no_limit(self):
        """bench 跑批自己控成本，不需要这层 —— 0 要能真的关掉。"""
        g = CostGuard(0)
        g.spend(10_000_000)
        g.check()
        self.assertEqual(g.remaining, -1)

    def test_message_says_how_to_change_it(self):
        g = CostGuard(10)
        g.spend(10)
        with self.assertRaises(BudgetExceeded) as cm:
            g.check()
        self.assertIn("--budget", str(cm.exception))

    def test_is_a_model_error_so_it_flows_through_existing_paths(self):
        """**这条是整个设计的支点。** BudgetExceeded 是 ModelError 的子类，
        携带 BUDGET_EXCEEDED 信号类型 —— 于是它和 LiteLLM 的硬拒绝走同一条路：
        Subagent 侧变硬信号交给架构师，架构师侧变「没有决策者」→ AWAITING_HUMAN。
        改成自定义异常的话，这些路径要重写一遍。
        """
        from cowork.signals import SignalType

        self.assertTrue(issubclass(BudgetExceeded, ModelError))
        self.assertIs(BudgetExceeded.signal_type, SignalType.BUDGET_EXCEEDED)

    def test_concurrent_spending_is_not_lost(self):
        """Scheduler 层内并行跑多个 Orchestrator，它们共享后端也共享这个计数器。"""
        g = CostGuard(0)

        def worker():
            for _ in range(200):
                g.spend(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(g.used, 8 * 200)


class TestBudgetedBackend(unittest.TestCase):
    def setUp(self):
        self.spec = _spec()

    def test_tokens_from_every_call_are_counted(self):
        g = CostGuard(0)
        b = BudgetedBackend(ScriptedBackend({}, token_cost=500), g)
        b.next_step(_ctx(self.spec))
        self.assertEqual(g.used, 500)
        b.summarize(_ctx(self.spec))
        self.assertGreater(g.used, 500)

    def test_call_is_refused_once_over(self):
        """**限额是停下来的水位线，不是总额上限。**

        检查在调用前、记账在调用后，而花钱之前无法知道这次要花多少 ——
        所以它最多超出一次调用的量。这里如实钉住这个语义：
        limit=600、每次 500 → 第 1 次放行（500）、第 2 次仍放行（越线到 1000）、
        第 3 次才拒。想让它更紧只能把 limit 调低，不能指望它按次预扣。
        """
        calls = {"n": 0}

        def verdict_for(*_):
            calls["n"] += 1
            from cowork.llm import ArchitectVerdict

            return ArchitectVerdict(action="CONTINUE", rationale="r", complexity_score=0.1)

        g = CostGuard(600)
        inner = ScriptedBackend({}, verdict_for=verdict_for, token_cost=500)
        b = BudgetedBackend(inner, g)

        b.decide_interrupt(self.spec, [], _ctx(self.spec))   # 500 < 600，放行
        b.decide_interrupt(self.spec, [], _ctx(self.spec))   # 越线到 1000
        with self.assertRaises(BudgetExceeded):
            b.decide_interrupt(self.spec, [], _ctx(self.spec))
        self.assertEqual(calls["n"], 2, "拒绝之后不该再打到真实后端")
        self.assertEqual(g.used, 1000, "超出量 = 最后那一次调用")

    def test_one_guard_shared_by_several_backends(self):
        """复核者和生成者是两个 Backend，但花的是同一笔钱。"""
        g = CostGuard(0)
        gen = BudgetedBackend(ScriptedBackend({}, token_cost=400), g)
        rev = BudgetedBackend(ScriptedBackend({}, token_cost=400), g)

        gen.next_step(_ctx(self.spec))
        rev.next_step(_ctx(self.spec))

        self.assertEqual(g.used, 800, "两个后端要记在同一本账上")

    def test_wrapper_is_transparent(self):
        """返回值一字不改，否则被包的后端行为会变。"""
        inner = ScriptedBackend({}, token_cost=123)
        b = BudgetedBackend(inner, CostGuard(0))
        direct_action, direct_tokens = ScriptedBackend({}, token_cost=123).next_step(
            _ctx(self.spec)
        )
        wrapped_action, wrapped_tokens = b.next_step(_ctx(self.spec))
        self.assertEqual(wrapped_tokens, direct_tokens)
        self.assertEqual(type(wrapped_action), type(direct_action))
        self.assertEqual(b.name, inner.name)

    def test_non_call_attributes_pass_through(self):
        """`cache_stats` 在真实后端上是**属性**（CacheStats 实例）不是方法。

        当成方法转发的话，`cli._report_cache` 会拿到一个函数对象然后
        `stats.calls` 炸掉 —— 实测踩过，所以这条用例存在。
        """
        from cowork.llm import CacheStats

        inner = ScriptedBackend({})
        inner.cache_stats = CacheStats()
        inner.cache_stats.calls = 3
        b = BudgetedBackend(inner, CostGuard(0))

        self.assertIsInstance(b.cache_stats, CacheStats)
        self.assertEqual(b.cache_stats.calls, 3)

    def test_forwards_the_whole_protocol(self):
        """加了新能力却忘了在这里转发 = 那条路径绕过护栏。

        用协议方法名逐个点一遍，缺一个就红。
        """
        b = BudgetedBackend(ScriptedBackend({}), CostGuard(0))
        for method in (
            "next_step", "triage", "decide_interrupt", "summarize", "verify",
            "review_decomposition", "review_spec_change", "decompose",
            "profile_tasks", "probe",
        ):
            self.assertTrue(hasattr(b, method), f"BudgetedBackend 少转发了 {method}")


if __name__ == "__main__":
    unittest.main()
