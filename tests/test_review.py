"""拆解复核（M5b，§12 M5 / §11.10）。

风险 #3 说架构师是唯一没被验证的环节。它的两半分别由不同机制管：

  中断决策那一半  §7.2 的确定性下限 + M5a 的停滞判据
  **拆解那一半**  就是这里

复核分两层，可信度不同，所以在数据结构上也是分开的两个字段：

  结构性检查  免费、确定性、不会漏判自己（依赖悬空、无 scope、有环、拆了等于没拆）
  验收标准反推 要花一次调用、可能假阳也可能假阴（「满足这些是否就等于完成原始目标」）

**这批测试只能验证机制接得对，验证不了模型判得准** —— 后者要真实模型，
见 §11.10 的实测记录。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork import demo_composite
from cowork.agent.architect import Architect
from cowork.llm.scripted import ScriptedBackend
from cowork.plan import deterministic_review
from cowork.policy import Policy
from cowork.store import SqliteStore
from cowork.types import Criterion, SandboxProfile, TaskClass, TaskSpec

GOAL = "做一个把文本行渲染成报告的小工具"


def spec(tid: str, *, deps=(), scope=("a.py",), goal="干活") -> TaskSpec:
    return TaskSpec(
        id=tid, goal=goal,
        acceptance=[Criterion("c1", "做完")],
        task_class=TaskClass.CODE,
        sandbox=SandboxProfile(workspace=tempfile.mkdtemp()),
        scope=list(scope), depends_on=list(deps),
    )


class TestDeterministicReview(unittest.TestCase):
    """免费那一半。这些缺陷不需要模型也能看出来，就不该花 token 去问。"""

    def test_clean_decomposition_has_no_structural_issues(self):
        issues = deterministic_review(
            GOAL,
            [spec("a", scope=("a.py",)), spec("b", scope=("b.py",))],
        )
        self.assertEqual(issues, [])

    def test_empty_decomposition(self):
        self.assertEqual(deterministic_review(GOAL, [])[0].kind, "empty")

    def test_subtask_without_scope_cannot_produce_anything(self):
        issues = deterministic_review(GOAL, [spec("a", scope=())])
        self.assertIn("no_scope", {i.kind for i in issues})

    def test_dangling_dependency(self):
        issues = deterministic_review(GOAL, [spec("a", deps=["ghost"])])
        self.assertIn("invalid_graph", {i.kind for i in issues})

    def test_cycle(self):
        issues = deterministic_review(
            GOAL, [spec("a", deps=["b"]), spec("b", deps=["a"])]
        )
        self.assertIn("invalid_graph", {i.kind for i in issues})

    def test_pure_chain_is_flagged(self):
        """拆了等于没拆 —— §1.4 第三条，顺序依赖强时多 agent 最差 −70%。"""
        issues = deterministic_review(
            GOAL, [spec("a"), spec("b", deps=["a"]), spec("c", deps=["b"])]
        )
        self.assertIn("fan_out", {i.kind for i in issues})


class TestReviewWiring(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore()

    def _architect(self, review_for=None) -> Architect:
        return Architect(
            ScriptedBackend({}, review_for=review_for), self.store, policy=Policy()
        )

    def test_structural_failure_skips_the_paid_review(self):
        """结构就是坏的，语义复核没有意义，也不该为它花 token。"""
        backend = ScriptedBackend({}, review_for=lambda g, s: (True, []))
        arch = Architect(backend, self.store, policy=Policy())

        result = arch.review_decomposition(GOAL, [spec("a", deps=["ghost"])])

        self.assertFalse(result.clean)
        self.assertEqual(backend.review_calls, 0, "结构坏了还去问模型")
        self.assertEqual(result.tokens, 0)

    def test_clean_structure_reaches_the_semantic_review(self):
        arch = self._architect(review_for=lambda g, s: (True, []))
        result = arch.review_decomposition(
            GOAL, [spec("a", scope=("a.py",)), spec("b", scope=("b.py",))]
        )
        self.assertTrue(result.clean)
        self.assertGreater(result.tokens, 0)

    def test_semantic_gap_is_reported_separately_from_structure(self):
        """两种可信度不同的证据不能抹平成一个布尔值。"""
        arch = self._architect(review_for=lambda g, s: (False, ["没有人负责格式化"]))
        result = arch.review_decomposition(
            GOAL, [spec("a", scope=("a.py",)), spec("b", scope=("b.py",))]
        )
        self.assertEqual(result.structural, [])
        self.assertFalse(result.sufficient)
        self.assertEqual(result.missing, ["没有人负责格式化"])
        self.assertFalse(result.clean)


class TestReviewInScheduler(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(getattr(self, "ws", ""), ignore_errors=True)

    def test_review_runs_before_dispatch(self):
        """拆错了就不该开跑 —— 跑完再发现就白烧了。"""
        seen: list[int] = []

        def review_for(goal, specs):
            seen.append(len(specs))
            return True, []

        sched, self.ws = demo_composite.build(
            backend=demo_composite.ScriptedComposite(review_for=review_for),
            log=lambda _m: None,
        )
        result = sched.run(max_cycles=2)

        self.assertEqual(seen, [4], "复核应在派发前跑且只跑一次")
        self.assertIsNotNone(result.review)
        self.assertTrue(result.review.clean)

    def test_no_root_goal_means_no_semantic_review(self):
        """没有原始目标就无从反推 —— 不猜，也不花那次调用。"""
        from cowork.scheduler import Scheduler

        sched, self.ws = demo_composite.build(log=lambda _m: None)
        bare = Scheduler(
            sched.specs, backend=demo_composite.ScriptedComposite(),
            store=SqliteStore(), log=lambda _m: None,
        )
        bare.run(max_cycles=2)
        self.assertIsNone(bare.review)

    def test_dropping_a_subtask_leaves_a_valid_but_degenerate_graph(self):
        """漏掉一环之后图仍然合法 —— 缺陷主要在语义层。

        顺带记一个免费的收获：少了并行的那一支之后，剩下的三个任务退化成一条链，
        结构检查的 `fan_out` **自己就先叫了一声**。它抓不到「缺了格式化」这件事，
        但能抓到「这个拆解已经没有并行度了」——零成本的一层筛子。
        """
        sched, self.ws = demo_composite.build(drop="t2_format", log=lambda _m: None)
        self.assertEqual(len(sched.specs), 3)

        issues = deterministic_review(demo_composite.ROOT_GOAL, sched.specs)
        kinds = {i.kind for i in issues}
        self.assertNotIn("invalid_graph", kinds, "依赖应已跟着摘干净")
        self.assertNotIn("no_scope", kinds)
        self.assertEqual(kinds, {"fan_out"})


if __name__ == "__main__":
    unittest.main()
