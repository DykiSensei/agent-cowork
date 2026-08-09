"""并行调度与冲突检测（§12 M4）。

这里守的第一条是架构不变量而不是功能：**并行度加在调度层，不能加在通信层**。
Subagent 之间仍然没有任何 API 面，下游拿到上游成果的唯一途径是调度器把
artifact 作为只读上下文注入（§8 / §1.4 第一条）。
"""

from __future__ import annotations

import shutil
import unittest

from cowork import demo_composite
from cowork.scheduler import Scheduler
from cowork.signals import SOFT_SIGNALS, SignalType, level_of
from cowork.types import TaskStatus


class TestConflictSignalLevel(unittest.TestCase):
    def test_detected_is_hard_suspected_is_soft(self):
        """确定性检出的冲突和 Subagent 的『怀疑』不该同级（§3.1）。"""
        self.assertEqual(level_of(SignalType.CONFLICT_DETECTED).value, "L0")
        self.assertEqual(level_of(SignalType.CONFLICT_SUSPECTED).value, "L1")
        self.assertIn(SignalType.CONFLICT_SUSPECTED, SOFT_SIGNALS)
        self.assertNotIn(SignalType.CONFLICT_DETECTED, SOFT_SIGNALS)

    def test_every_task_class_can_produce_it(self):
        """冲突由调度器在产出层检出，与任务本身有没有内容层判据无关。

        GENERATIVE 正因为几乎没有内容层判据，才更需要这条兜底（§3.2.1）。
        """
        from cowork.signals import default_hard_signals

        for cls in ("CODE", "TOOL_CALL", "GENERATIVE"):
            self.assertIn(SignalType.CONFLICT_DETECTED, default_hard_signals(cls))


class CompositeFixture(unittest.TestCase):
    def setUp(self):
        self.sched, self.ws = demo_composite.build(log=lambda _m: None)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)


class TestCompositeRun(CompositeFixture):
    def test_plan_shape_meets_m4_exit_criteria(self):
        """出口标准：3–5 个子任务，至少两种 task_class，真有并行度。"""
        specs = self.sched.specs
        self.assertGreaterEqual(len(specs), 3)
        self.assertLessEqual(len(specs), 5)
        self.assertGreaterEqual(len({s.task_class for s in specs}), 2)

        plan = self.sched.plan
        self.assertEqual([[t.id for t in x] for x in plan.layers],
                         [["t1_parse", "t2_format"], ["t3_report"], ["t4_check"]])
        self.assertTrue(plan.decomposable)
        self.assertEqual(plan.issues, [])

    def test_all_subtasks_complete(self):
        result = self.sched.run(max_cycles=3)

        self.assertTrue(result.completed, result.to_dict()["tasks"])
        self.assertEqual(len(result.results), 4)
        for tid in ("t1_parse", "t2_format", "t3_report", "t4_check"):
            self.assertIs(result.results[tid].state.status, TaskStatus.COMPLETED)

    def test_downstream_gets_upstream_artifacts_as_readonly_context(self):
        """Subagent 之间不通信；上游产出由调度器注入（§1.4 第一条 / §8）。"""
        result = self.sched.run(max_cycles=3)

        injected = result.results["t3_report"].context.injected
        refs = {a.content_ref for a in injected}
        self.assertEqual(refs, {"parse.py", "formatter.py"})

    def test_no_conflict_when_scopes_are_disjoint(self):
        result = self.sched.run(max_cycles=3)
        self.assertEqual(result.conflicts, [])

    def test_result_is_json_serializable(self):
        import json

        json.dumps(self.sched.run(max_cycles=3).to_dict(), ensure_ascii=False)


class TestConflictDetection(unittest.TestCase):
    """同层并行写同一份产出 —— 静态检查看不到的那种。

    scope 声明层面的交集在 build_plan 就被串行化了，所以运行期还能撞上的只有
    一种：架构师在运行中用 MODIFY_TASK 改宽了 scope。这个后端就是照着那条路径写的。
    """

    def setUp(self):
        self.sched, self.ws = demo_composite.build(
            backend=demo_composite.ScriptedComposite(collide=True), log=lambda _m: None
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_same_layer_double_write_is_detected_and_arbitrated(self):
        result = self.sched.run(max_cycles=3)

        self.assertTrue(result.conflicts, "同层并行写同一个文件没被检出")
        sig = result.conflicts[0]
        self.assertIs(sig.type, SignalType.CONFLICT_DETECTED)
        self.assertEqual(sig.payload["resource"], "parse.py")
        self.assertEqual(sorted(sig.payload["tasks"]), ["t1_parse", "t2_format"])

        # 仲裁不新开决策通道，走的是既有的 Architect.decide()
        self.assertTrue(result.arbitrations)
        self.assertEqual(result.arbitrations[0]["resource"], "parse.py")

    def test_conflict_is_not_reported_twice(self):
        result = self.sched.run(max_cycles=3)
        keys = [(s.payload["resource"], tuple(s.payload["tasks"])) for s in result.conflicts]
        self.assertEqual(len(keys), len(set(keys)))


class TestCrossLayerIsNotAConflict(unittest.TestCase):
    def test_sequential_handoff_is_normal(self):
        """跨层写同一个文件是有序交接，不是冲突 —— 误报会让正常拆解跑不动。"""
        sched, ws = demo_composite.build(log=lambda _m: None)
        try:
            # t3 依赖 t1/t2，把它的 scope 改成和 t1 相同：跨层，不该报冲突
            specs = sched.specs
            specs[2] = specs[2].bump(revision=1, scope=["parse.py"])
            sched2 = Scheduler(
                specs, backend=demo_composite.ScriptedComposite(),
                store=sched.store, log=lambda _m: None,
            )
            self.assertEqual(sched2.plan.issues, [], "跨层不该触发静态串行化")
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
