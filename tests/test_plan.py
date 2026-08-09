"""任务图：拓扑分层、可分解性、静态冲突（§12 M4 的 4.2 / 4.3）。

全部确定性，不需要模型。这些判断错了的后果都是静默的 ——
并行写同一个文件不会报错，只会让先写的那份产出消失。
"""

from __future__ import annotations

import tempfile
import unittest

from cowork.plan import PlanError, build_plan, scope_overlaps, topo_layers
from cowork.types import Criterion, SandboxProfile, TaskClass, TaskSpec


def spec(tid: str, *, deps=(), scope=("a.py",), cls=TaskClass.CODE) -> TaskSpec:
    return TaskSpec(
        id=tid,
        goal=f"任务 {tid}",
        acceptance=[Criterion("c1", "做完")],
        task_class=cls,
        sandbox=SandboxProfile(workspace=tempfile.mkdtemp()),
        scope=list(scope),
        depends_on=list(deps),
    )


class TestTopoLayers(unittest.TestCase):
    def test_independent_tasks_share_one_layer(self):
        layers = topo_layers([spec("a", scope=("a.py",)), spec("b", scope=("b.py",))])
        self.assertEqual([[t.id for t in x] for x in layers], [["a", "b"]])

    def test_chain_becomes_one_task_per_layer(self):
        layers = topo_layers(
            [spec("a"), spec("b", deps=["a"]), spec("c", deps=["b"])]
        )
        self.assertEqual([[t.id for t in x] for x in layers], [["a"], ["b"], ["c"]])

    def test_diamond(self):
        layers = topo_layers(
            [
                spec("a", scope=("a.py",)),
                spec("b", deps=["a"], scope=("b.py",)),
                spec("c", deps=["a"], scope=("c.py",)),
                spec("d", deps=["b", "c"], scope=("d.py",)),
            ]
        )
        self.assertEqual([[t.id for t in x] for x in layers], [["a"], ["b", "c"], ["d"]])

    def test_cycle_is_rejected_not_worked_around(self):
        """环意味着拆解本身错了，不能靠运行时兜底。"""
        with self.assertRaises(PlanError) as cm:
            topo_layers([spec("a", deps=["b"]), spec("b", deps=["a"])])
        self.assertIn("环", str(cm.exception))

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaises(PlanError):
            topo_layers([spec("a", deps=["nope"])])

    def test_layering_is_reproducible(self):
        tasks = [spec("c", scope=("c.py",)), spec("a", scope=("a.py",)),
                 spec("b", scope=("b.py",))]
        first = [[t.id for t in x] for x in topo_layers(tasks)]
        second = [[t.id for t in x] for x in topo_layers(list(reversed(tasks)))]
        self.assertEqual(first, second)


class TestScopeOverlap(unittest.TestCase):
    def test_identical_scope(self):
        hits = scope_overlaps([spec("a"), spec("b")])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][:2], ("a", "b"))

    def test_glob_counts_as_overlap(self):
        """`*.py` 与 `solution.py` 必须算冲突 —— 漏报的代价是产出被覆盖。"""
        hits = scope_overlaps([spec("a", scope=("*.py",)), spec("b", scope=("solution.py",))])
        self.assertEqual(len(hits), 1)

    def test_disjoint_scope_is_clean(self):
        self.assertEqual(scope_overlaps([spec("a", scope=("a.py",)),
                                         spec("b", scope=("b.py",))]), [])


class TestBuildPlan(unittest.TestCase):
    def test_conflicting_layer_is_serialized(self):
        """并行写同一个文件是静默失败，宁可串行。"""
        plan = build_plan([spec("a", scope=("shared.py",)), spec("b", scope=("shared.py",))])
        self.assertEqual([[t.id for t in x] for x in plan.layers], [["a"], ["b"]])
        kinds = {i.kind for i in plan.issues}
        self.assertIn("scope_overlap", kinds)
        self.assertIn("serialized", kinds)

    def test_clean_layer_stays_parallel(self):
        plan = build_plan([spec("a", scope=("a.py",)), spec("b", scope=("b.py",))])
        self.assertEqual(plan.max_parallel, 2)
        self.assertTrue(plan.decomposable)
        self.assertEqual(plan.issues, [])

    def test_pure_chain_is_flagged_as_not_decomposable(self):
        """§1.4 第三条：顺序依赖强的任务多 agent 最差 −70%，该退化为单 agent。"""
        plan = build_plan([spec("a"), spec("b", deps=["a"]), spec("c", deps=["b"])])
        self.assertFalse(plan.decomposable)
        self.assertIn("fan_out", {i.kind for i in plan.issues})

    def test_single_task_is_not_flagged(self):
        plan = build_plan([spec("a")])
        self.assertEqual(plan.issues, [])


if __name__ == "__main__":
    unittest.main()
