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

    def make(self, steps, *, scope=("out.py",), max_steps=6, acceptance=None):
        spec = TaskSpec(
            goal="写文件",
            acceptance=acceptance or [Criterion("c1", "写出来就行")],
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

    def test_probe_read_of_missing_file_does_not_preempt(self):
        """探测一个还不存在的文件不是任务级失败（§11.6a）。

        M2 实测里这是最大的单一噪声源：Subagent 几乎总是先 read_file 探一下产出
        文件在不在，「不在」被当成 TOOL_FAILURE 抢占，每个任务白烧一轮架构师决策。
        失败结果照样进 reasoning_trace 回给模型，只是不产生硬信号。
        """
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("read_file", {"path": "out.py"}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
                (1, 2): Finish(output={}, summary="写完了"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        self.assertIsNone(outcome.preempting_signal)
        probe = next(e for e in ctx.reasoning_trace if e.get("name") == "read_file")
        self.assertFalse(probe["ok"], "失败事实仍要如实回给模型")

    def test_self_rehearsal_of_acceptance_command_does_not_preempt(self):
        """Subagent 自己预演验收命令失败，不该把架构师叫来（§11.6e）。

        验收的判定权归 Runtime 在 Finish 之后行使 —— 那一次失败仍然产生
        TEST_FAILED（见下一个测试），所以信号覆盖面一条没少，少的只是
        「架构师被叫来说一句继续」的那 3.5k token。
        """
        crit = Criterion("c1", "跑得过", ["python", "-c", "import sys; sys.exit(1)"])
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("run", {"command": list(crit.command)}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
            },
            acceptance=[crit],
            max_steps=2,
        )
        outcome = loop.run(ctx, agent)

        # 两个 step 都跑完了才因 STEP_LIMIT 停 —— 说明第一步没抢占
        self.assertIs(outcome.preempting_signal.type, SignalType.STEP_LIMIT)
        rehearsal = ctx.reasoning_trace[1]
        self.assertEqual(rehearsal["name"], "run")
        self.assertFalse(rehearsal["ok"], "失败事实仍要如实回给模型")

    def test_acceptance_still_fails_at_finish(self):
        """预演不抢占，不等于验收失效。Finish 之后那次仍然产生 TEST_FAILED。"""
        crit = Criterion("c1", "跑得过", ["python", "-c", "import sys; sys.exit(1)"])
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("run", {"command": list(crit.command)}),
                (1, 1): Finish(output={}, summary="自认为好了"),
            },
            acceptance=[crit],
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.TEST_FAILED)

    def test_unrelated_command_failure_still_preempts(self):
        """只有验收命令本身豁免。别的命令炸了仍然是 TOOL_FAILURE。"""
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("run", {"command": ["python", "-c", "import sys; sys.exit(9)"]})},
            acceptance=[Criterion("c1", "跑得过", ["python", "verify.py"])],
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.TOOL_FAILURE)

    def test_list_files_replaces_the_ls_workaround(self):
        """没有 list_files 时真实 agent 只能去 run 一个 ls，然后越界（§11.6f）。"""
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("list_files", {"path": "."}),
                (1, 1): Finish(output={}, summary="看过了"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        listing = ctx.reasoning_trace[1]
        self.assertTrue(listing["ok"])
        self.assertIn("protected.txt", listing["stdout"])

    def test_ls_is_still_a_scope_violation(self):
        """加了 list_files 不等于放开 allowed_binaries。"""
        loop, agent, ctx = self.make({(1, 0): ToolCall("run", {"command": ["ls"]})})
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)

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
