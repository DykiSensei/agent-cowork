"""取消：人要求停下来（M6 §9 原来欠的那条）。

和 `intervene` 的区别是这一层的全部意义：介入说「换个做法接着干」，取消说
「别干了」。所以取消**不问架构师** —— 让它去裁决一件人已经拍板的事，它有
可能回 CONTINUE，那时候「取消」就降级成了一个建议。

停下来的时机仍然是 step 边界（§10.1 地基不动），所以这里同时钉住：
已经写出来的产出不会因为取消而消失。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.llm.scripted import ScriptedBackend
from cowork.orchestrator import Orchestrator
from cowork.store import SqliteStore
from cowork.types import (
    Action,
    Criterion,
    Decider,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskStatus,
)


class _NoArchitect(ScriptedBackend):
    """架构师被调用就是失败 —— 取消路径上它一次都不该出现。"""

    def decide_interrupt(self, *a, **kw):  # noqa: D102
        raise AssertionError("取消之后不该再问架构师")


class _CancelAtStep(_NoArchitect):
    """跑到第 n 步时替人按下取消。

    模拟的是「任务跑到一半，人在界面上点了停」—— 用脚本触发是为了确定性，
    真实路径是 HTTP 端点调 `Runner.cancel()`。
    """

    def __init__(self, steps, *, at: int, **kw):
        super().__init__(steps, **kw)
        self._at = at
        self._n = 0
        self.orch: Orchestrator | None = None

    def next_step(self, ctx):
        self._n += 1
        if self._n == self._at and self.orch is not None:
            self.orch.cancel("不用做了，方向变了")
        return super().next_step(ctx)


class CancelFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-cancel-"))
        self.store = SqliteStore()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def spec(self) -> TaskSpec:
        return TaskSpec(
            goal="写一个脚本",
            parent_id="task_parent",  # 避开 §7.2 的顶层保护
            acceptance=[Criterion("c1", "写出来就行")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=["out.py"],
            max_steps=8,
        )

    def orch(self, backend) -> Orchestrator:
        return Orchestrator(
            self.spec(), backend=backend, store=self.store, log=lambda _m: None
        )


class TestCancel(CancelFixture):
    def test_cancel_before_start_ends_abandoned_without_asking_architect(self):
        """两个 cycle 之间到达的取消 —— 循环开头那道检查。"""
        orch = self.orch(_NoArchitect({(1, 0): Finish(output={}, summary="做完了")}))
        orch.cancel("不做了")

        result = orch.run()

        self.assertIs(result.state.status, TaskStatus.ABANDONED)
        self.assertEqual(result.state.current_step, 0, "取消应该在起 Subagent 之前生效")

    def test_cancel_mid_run_stops_at_step_boundary(self):
        backend = _CancelAtStep(
            {
                (1, 0): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 2"}),
                (1, 2): Finish(output={}, summary="做完了"),
            },
            at=2,
        )
        orch = self.orch(backend)
        backend.orch = orch

        result = orch.run()

        self.assertIs(result.state.status, TaskStatus.ABANDONED)
        # **已经在飞的那个 step 会跑完**：抢占检查在 step 开头，取消到达时
        # 第 2 步已经进了执行。所以盘上是 x = 2，不是 x = 1 —— 取消是「停下来」
        # 不是「回滚」，产出一律保留（实测这个等待中位 1.65s / p95 3.11s）。
        self.assertEqual((self.ws / "out.py").read_text(encoding="utf-8"), "x = 2")
        # 但第 3 步（Finish）没有发生：真的停住了，不是跑完了
        self.assertIsNot(result.state.status, TaskStatus.COMPLETED)

    def test_cancel_records_a_human_decision(self):
        """ABANDONED 在界面上和「架构师主动放弃」是同一个终局 ——
        不写裁决记录的话，时间线上只剩一个没有来由的终止。
        """
        orch = self.orch(_NoArchitect({(1, 0): Finish(output={}, summary="做完了")}))
        orch.cancel("预算不够了")
        result = orch.run()

        self.assertEqual(len(result.decisions), 1)
        record = result.decisions[0]
        self.assertIs(record.decider, Decider.HUMAN)
        self.assertIs(record.action, Action.ABANDON)
        self.assertIn("预算不够了", record.rationale)
        # 取消不改 spec —— 它不构成第二个写入点（§2.3）
        self.assertIsNone(record.new_spec)

    def test_cancel_writes_a_human_event(self):
        """人说的话要渲染成对话气泡，不是一行日志。"""
        orch = self.orch(_NoArchitect({(1, 0): Finish(output={}, summary="做完了")}))
        orch.cancel("停")
        orch.run()

        kinds = [e.kind for e in self.store.events_for(orch.spec.id)]
        self.assertIn("human", kinds)
        human = [e for e in self.store.events_for(orch.spec.id) if e.kind == "human"][0]
        self.assertIn("停", human.text)

    def test_default_reason_is_not_empty(self):
        """理由是空串时也要有话可展示，别在界面上留一条空记录。"""
        orch = self.orch(_NoArchitect({(1, 0): Finish(output={}, summary="做完了")}))
        orch.cancel("")
        result = orch.run()
        self.assertTrue(result.decisions[0].rationale.strip())


if __name__ == "__main__":
    unittest.main()
