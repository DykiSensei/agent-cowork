"""TaskSpec 的硬约束（§4.1）与升级边界的确定性下限（§7.2）。"""

import unittest

from cowork.escalation import deterministic_escalation, should_escalate
from cowork.llm import ArchitectVerdict
from cowork.policy import Policy
from cowork.signals import SignalSource, SignalType
from cowork.types import (
    Criterion,
    SandboxProfile,
    Signal,
    SilencePolicy,
    TaskClass,
    TaskSpec,
    TaskState,
)

SANDBOX = SandboxProfile(workspace=".")


class TestSpecConstraints(unittest.TestCase):
    def test_acceptance_is_mandatory(self):
        with self.assertRaises(ValueError):
            TaskSpec(goal="做点什么", acceptance=[], task_class=TaskClass.GENERATIVE,
                     probe_interval_s=30)

    def test_code_requires_sandbox(self):
        with self.assertRaises(ValueError):
            TaskSpec(goal="写代码", acceptance=[Criterion("c1", "跑通")],
                     task_class=TaskClass.CODE)

    def test_generative_is_forced_to_probe(self):
        """架构师无权把 GENERATIVE 设为 TRUST（§4.1）。"""
        spec = TaskSpec(
            goal="写一篇调研",
            acceptance=[Criterion("c1", "覆盖三个来源")],
            task_class=TaskClass.GENERATIVE,
            silence_policy=SilencePolicy.TRUST,   # 试图绕过
            probe_interval_s=30,
        )
        self.assertIs(spec.silence_policy, SilencePolicy.PROBE)

    def test_probe_requires_interval(self):
        with self.assertRaises(ValueError):
            TaskSpec(goal="写一篇调研", acceptance=[Criterion("c1", "x")],
                     task_class=TaskClass.GENERATIVE)

    def test_hard_signal_coverage_differs_by_class(self):
        """§3.2.1：不同 task_class 的硬信号覆盖面差别极大。"""
        code = TaskSpec(goal="g", acceptance=[Criterion("c", "d")],
                        task_class=TaskClass.CODE, sandbox=SANDBOX)
        gen = TaskSpec(goal="g", acceptance=[Criterion("c", "d")],
                       task_class=TaskClass.GENERATIVE, probe_interval_s=30)

        self.assertIn(SignalType.TEST_FAILED, code.hard_signals)
        self.assertNotIn(SignalType.TEST_FAILED, gen.hard_signals,
                         "GENERATIVE 几乎无内容层判据")
        self.assertIn(SignalType.HUMAN_INTERVENTION, gen.hard_signals,
                      "人的介入对任何 task_class 都必须可抢占")
        self.assertLess(len(gen.hard_signals), len(code.hard_signals))

    def test_bump_increments_revision(self):
        s = TaskSpec(goal="g", acceptance=[Criterion("c", "d")],
                     task_class=TaskClass.CODE, sandbox=SANDBOX)
        self.assertEqual(s.bump().revision, 2)
        self.assertEqual(s.revision, 1, "bump 不改原对象")

    def test_roundtrip(self):
        s = TaskSpec(goal="g", acceptance=[Criterion("c", "d", ["python", "v.py"])],
                     task_class=TaskClass.CODE, sandbox=SANDBOX, scope=["a.py"])
        again = TaskSpec.from_dict(s.to_dict())
        self.assertEqual(again.to_dict(), s.to_dict())


def spec(**kw) -> TaskSpec:
    base = dict(goal="g", acceptance=[Criterion("c", "d")],
                task_class=TaskClass.CODE, sandbox=SANDBOX, token_budget=1000)
    base.update(kw)
    return TaskSpec(**base)


def sig(t: SignalType, task_id: str) -> Signal:
    return Signal(type=t, task_id=task_id, source=SignalSource.RUNTIME)


class TestEscalation(unittest.TestCase):
    def setUp(self):
        self.policy = Policy()
        self.calm = ArchitectVerdict(action="CONTINUE", rationale="重试", complexity_score=0.1)

    def test_low_complexity_child_task_stays_with_llm(self):
        s = spec(parent_id="task_parent")
        st = TaskState(spec=s)
        self.assertIsNone(should_escalate(self.policy, s, st, [], self.calm))

    def test_high_complexity_escalates(self):
        s = spec(parent_id="task_parent")
        st = TaskState(spec=s)
        v = ArchitectVerdict(action="CONTINUE", rationale="不确定", complexity_score=0.9)
        self.assertIn("complexity_score", should_escalate(self.policy, s, st, [], v))

    def test_toplevel_modify_escalates_regardless_of_confidence(self):
        """LLM 给了低分也没用——这条与 complexity_score 是「或」的关系。"""
        s = spec(parent_id=None)
        st = TaskState(spec=s)
        v = ArchitectVerdict(action="MODIFY_TASK", rationale="小改", complexity_score=0.01)
        reason = should_escalate(self.policy, s, st, [], v)
        self.assertIsNotNone(reason)
        self.assertIn("顶层任务", reason)

    def test_repeated_interrupts_escalate(self):
        s = spec(parent_id="p")
        st = TaskState(spec=s, interrupt_count=3)
        reason = deterministic_escalation(self.policy, s, st, [], self.calm)
        self.assertIn("interrupt_count", reason)

    def test_scope_violation_escalates(self):
        s = spec(parent_id="p")
        st = TaskState(spec=s)
        reason = deterministic_escalation(
            self.policy, s, st, [sig(SignalType.SCOPE_VIOLATION, s.id)], self.calm
        )
        self.assertIn("SCOPE_VIOLATION", reason)

    def test_budget_overrun_escalates(self):
        s = spec(parent_id="p")
        st = TaskState(spec=s, tokens_used=900)  # 90% > 80%
        reason = deterministic_escalation(self.policy, s, st, [], self.calm)
        self.assertIn("预算", reason)

    def test_irreversible_operation_escalates(self):
        s = spec(parent_id="p", acceptance=[Criterion("c", "部署后可访问",
                                                      ["kubectl", "apply", "-f", "x.yaml"])])
        st = TaskState(spec=s)
        reason = deterministic_escalation(self.policy, s, st, [], self.calm)
        self.assertIn("不可逆", reason)


if __name__ == "__main__":
    unittest.main()
