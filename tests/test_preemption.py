"""step 边界抢占：这是「外部中断」的全部实现（§10.1 / §5.1）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.agent.subagent import Subagent
from cowork.llm.scripted import ScriptedBackend
from cowork.runtime.bus import SignalBus
from cowork.runtime.loop import StepLoop
from cowork.runtime.sandbox import Sandbox
from cowork.signals import SignalType
from cowork.store import SqliteStore
from cowork.types import (
    AgentContext,
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskStatus,
)


class LoopFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-test-"))
        (self.ws / "protected.txt").write_text("不该被改", encoding="utf-8")
        self.store = SqliteStore()
        self.bus = SignalBus()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def make(self, steps, *, scope=("out.py",), max_steps=6):
        spec = TaskSpec(
            goal="写文件",
            acceptance=[Criterion("c1", "写出来就行")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=list(scope),
            max_steps=max_steps,
        )
        self.store.save_task(__import__("cowork").TaskState(spec=spec))
        sandbox = Sandbox(spec.sandbox, spec.scope)
        loop = StepLoop(bus=self.bus, sandbox=sandbox, store=self.store)
        agent = Subagent(ScriptedBackend(steps))
        return loop, agent, AgentContext(task_spec=spec)


class TestPreemption(LoopFixture):
    def test_human_intervention_preempts_before_first_step(self):
        """人的介入视同硬信号，最高优先级，立即抢占（§2.4）。"""
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("write_file", {"path": "out.py", "content": "x = 1"})}
        )
        self.bus.human_intervention(ctx.task_spec.id, "先停一下，方向要改")

        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertEqual(outcome.steps_run, 0, "抢占发生在派发下一个 step 之前")
        self.assertIs(outcome.preempting_signal.type, SignalType.HUMAN_INTERVENTION)
        self.assertFalse((self.ws / "out.py").exists(), "被抢占时不应有副作用")

    def test_human_intervention_mid_run(self):
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("write_file", {"path": "out.py", "content": "a"}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "b"}),
                (1, 2): Finish(output={}, summary="done"),
            }
        )

        original = agent.next_step

        def intercept(c):
            action, cost = original(c)
            # 第一个 step 执行完之后，人介入
            self.bus.human_intervention(c.task_spec.id, "停")
            agent.next_step = original
            return action, cost

        agent.next_step = intercept
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertEqual(outcome.steps_run, 1, "跑完当前 step 才停")
        self.assertIs(outcome.preempting_signal.type, SignalType.HUMAN_INTERVENTION)
        self.assertIsNotNone(outcome.checkpoint_id, "中断处必须有 checkpoint")


class TestHardSignals(LoopFixture):
    def test_scope_violation(self):
        """SCOPE_VIOLATION 兼作安全边界和跑偏探测器（§3.2 设计注记）。"""
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("write_file", {"path": "protected.txt", "content": "改了"})}
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)
        self.assertEqual(
            (self.ws / "protected.txt").read_text(encoding="utf-8"),
            "不该被改",
            "越界写入必须在落盘前被拦截",
        )

    def test_path_escape_is_scope_violation(self):
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("write_file", {"path": "../escaped.py", "content": "x"})}
        )
        outcome = loop.run(ctx, agent)
        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)

    def test_step_limit(self):
        never_finishes = {
            (1, i): ToolCall("write_file", {"path": "out.py", "content": str(i)})
            for i in range(20)
        }
        loop, agent, ctx = self.make(never_finishes, max_steps=3)
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertIs(outcome.preempting_signal.type, SignalType.STEP_LIMIT)
        self.assertEqual(outcome.steps_run, 3)

    def test_tool_failure(self):
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("run", {"command": ["python", "-c", "import sys; sys.exit(3)"]})}
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.TOOL_FAILURE)
        self.assertEqual(outcome.preempting_signal.payload["exit_code"], 3)

    def test_validation_failed_on_bad_output(self):
        spec_steps = {(1, 0): Finish(output={"wrong": 1}, summary="乱填")}
        loop, agent, ctx = self.make(spec_steps)
        ctx.task_spec = ctx.task_spec.bump(
            revision=1,
            output_schema={
                "type": "object",
                "properties": {"file": {"type": "string"}},
                "required": ["file"],
            },
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.VALIDATION_FAILED)
        self.assertTrue(outcome.preempting_signal.payload["errors"])


class TestSoftSignalsDoNotPreempt(LoopFixture):
    def test_soft_signal_is_queued_not_preempting(self):
        from cowork.actions import SoftSignalAction

        loop, agent, ctx = self.make(
            {
                (1, 0): SoftSignalAction("AMBIGUITY", "goal 里没说要不要处理空串"),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
                (1, 2): Finish(output={}, summary="done"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED, "软信号无权要求中断")
        self.assertEqual(len(outcome.soft_signals), 1)
        self.assertEqual(outcome.soft_signals[0].level.value, "L1")
        self.assertEqual(outcome.soft_signals[0].source.value, "SUBAGENT")


if __name__ == "__main__":
    unittest.main()
