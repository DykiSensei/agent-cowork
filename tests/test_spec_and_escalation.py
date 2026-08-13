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

    def test_hard_signals_is_a_declaration_not_a_filter(self):
        """**Runtime 不查这个集合就发信号** —— 这是刻意的，不是漏了。

        `hard_signals` 说的是「这一类任务**预期**能产生哪些硬信号」，它的读者是
        `__post_init__`（据此把 GENERATIVE 强制成 PROBE）和界面（显示覆盖面）。
        真要拿它当过滤器，一个 GENERATIVE 任务把工具跑挂了就不会中断 ——
        漏报一条真实的失败，比多报一条超出预期的失败贵得多。

        钉住它是因为两种读法都说得通，而选错的代价是静默丢信号。
        **断言的是行为不是源码文本**：上一版查 loop.py 里有没有出现
        "hard_signals" 这个词，结果一句注释就把它弄红了 —— 那种断言测的是措辞。
        """
        import shutil
        import tempfile

        from cowork.actions import ToolCall
        from cowork.llm.scripted import ScriptedBackend
        from cowork.runtime.bus import SignalBus
        from cowork.runtime.loop import StepLoop
        from cowork.runtime.sandbox import Sandbox
        from cowork.store import SqliteStore
        from cowork.types import AgentContext, SandboxProfile, TaskState

        gen_ws = tempfile.mkdtemp(prefix="cowork-gen-")
        try:
            gen = TaskSpec(
                goal="写一篇调研", acceptance=[Criterion("c", "d")],
                task_class=TaskClass.GENERATIVE, probe_interval_s=30,
                sandbox=SandboxProfile(workspace=gen_ws, allowed_binaries=("python",)),
                scope=["out.md"],
            )
            self.assertNotIn(SignalType.TOOL_FAILURE, gen.hard_signals,
                             "GENERATIVE 的声明里本来就没有它")

            store = SqliteStore()
            store.save_task(TaskState(spec=gen))
            loop = StepLoop(bus=SignalBus(),
                            sandbox=Sandbox(gen.sandbox, gen.scope), store=store)
            agent = ScriptedBackend({
                (1, 0): ToolCall(
                    name="run",
                    args={"command": ["python", "-c", "import sys;sys.exit(3)"]},
                    thought="跑一下",
                ),
            })
            outcome = loop.run(AgentContext(task_spec=gen), agent)

            # 声明里没有 TOOL_FAILURE，但它真的发生了 —— 就该照样中断
            self.assertIs(outcome.preempting_signal.type, SignalType.TOOL_FAILURE)
        finally:
            shutil.rmtree(gen_ws, ignore_errors=True)

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

    def test_acceptance_scope_violation_stays_with_the_architect(self):
        """验收 command 撞白名单（during=acceptance）不该升级给人 —— 那是架构师
        生成的验收脚本写错了程序（test/sh），该让架构师决策换 command；升级给人的话，
        人那边没有换 command 的入口，只会反复「改一下任务」而 spec 纹丝不动。"""
        s = spec(parent_id="p")
        st = TaskState(spec=s)
        acceptance_sig = Signal(
            type=SignalType.SCOPE_VIOLATION, task_id=s.id,
            source=SignalSource.RUNTIME, payload={"during": "acceptance"},
        )
        reason = deterministic_escalation(
            self.policy, s, st, [acceptance_sig], self.calm
        )
        self.assertIsNone(reason)

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
