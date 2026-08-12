"""终局之后还能改（M12，§11.30）。

真实使用里最常见的形状：任务跑完了，产出不完全对，人想说「再加一节」。
原来这件事做不到 —— `intervene` 只对**活着的循环**有效（要有 step 边界来取
抢占队列），`ruling` 只认 AWAITING_HUMAN。人手上什么都没有，只能重发一个任务，
而那会丢掉已经做出来的产物和整条执行记录。

这里钉住的是：**追加要求没有走新机制**。它就是 `intervene` 那条路
（HUMAN_INTERVENTION 硬信号 → 架构师 decide → 既有的 apply_resume），
所以写入侧的全部约束（唯一写入决策点、升级下限、写入侧复核）自动成立。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.llm import ArchitectVerdict
from cowork.llm.scripted import ScriptedBackend
from cowork.orchestrator import Orchestrator
from cowork.store import SqliteStore
from cowork.signals import SignalType
from cowork.types import (
    Criterion,
    Decider,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskStatus,
)

QUIET = lambda _m: None  # noqa: E731


def _add_a_section(_spec, _signals) -> ArchitectVerdict:
    """架构师对追加要求的典型反应：把它变成一条新的验收标准。

    **注意人没有直接改 spec** —— 他给的是一句话，改成什么样是架构师决定的。
    """
    return ArchitectVerdict(
        action="MODIFY_TASK",
        rationale="人要求补一节说明，加一条验收标准并继续",
        complexity_score=0.2,
        spec_changes={"added_criteria": ["README 里要有「用法」一节"]},
    )


class FollowUpFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-followup-"))
        self.store = SqliteStore()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def spec(self) -> TaskSpec:
        return TaskSpec(
            goal="写一个 README",
            parent_id="task_parent",  # 避开 §7.2 的顶层保护
            acceptance=[Criterion("c1", "有文件")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws)),
            scope=["README.md"],
            max_steps=8,
        )

    def _completed(self, verdict_for=_add_a_section):
        """跑出一个 COMPLETED 的任务，返回 (orch, backend)。"""
        backend = ScriptedBackend(
            {
                (1, 0): ToolCall("write_file", {"path": "README.md", "content": "# 标题\n"}),
                (1, 1): Finish(output={}, summary="第一版写完了"),
                # 续跑之后 revision=2，脚本从这里接着走
                (2, 0): ToolCall(
                    "write_file",
                    {"path": "README.md", "content": "# 标题\n\n## 用法\n跑 main.py\n"},
                ),
                (2, 1): Finish(output={}, summary="补上了用法一节"),
            },
            verdict_for=verdict_for,
        )
        orch = Orchestrator(
            self.spec(), backend=backend, store=self.store, log=QUIET
        )
        result = orch.run()
        self.assertIs(result.state.status, TaskStatus.COMPLETED)
        return orch, backend


class TestFollowUp(FollowUpFixture):
    def test_a_finished_task_can_be_asked_for_more(self):
        orch, _ = self._completed()

        result = orch.follow_up("再补一节「用法」")

        self.assertIs(result.state.status, TaskStatus.COMPLETED)
        self.assertEqual(result.state.spec.revision, 2, "续跑要走 revision")
        self.assertIn(
            "用法",
            (self.ws / "README.md").read_text(encoding="utf-8"),
            "续跑得真的改到产物上",
        )

    def test_the_work_already_done_is_kept(self):
        """原地续跑相对「重发一个任务」的全部价值就在这里。"""
        orch, _ = self._completed()
        before = {a.content_ref for a in orch.ctx.produced}

        orch.follow_up("再补一节「用法」")

        self.assertTrue(before, "第一轮本来就该有产出")
        self.assertLessEqual(
            before,
            {a.content_ref for a in orch.ctx.produced},
            "第一轮的产物不能因为续跑而消失",
        )
        self.assertTrue(
            any(a.kind == "summary" for a in orch.ctx.injected),
            "上一轮的执行记录要以摘要的形式带过来（REBASE 的意义）",
        )

    def test_the_human_words_become_a_signal_not_a_spec_edit(self):
        """写权不变（§2.3）：人给的是信号，新 spec 由架构师构造。"""
        orch, _ = self._completed()

        orch.follow_up("再补一节「用法」")

        sigs = [
            s for s in self.store.signals_for(orch.spec.id)
            if s.type is SignalType.HUMAN_INTERVENTION
        ]
        self.assertEqual([s.payload["instruction"] for s in sigs], ["再补一节「用法」"])
        decision = orch.decisions[-1]
        self.assertIs(decision.decider, Decider.HUMAN)
        self.assertIsNotNone(decision.new_spec, "裁决要带着新 spec，那是写入那一步")

    def test_the_original_goal_is_kept_and_the_request_is_appended(self):
        """**追加不是替换。**

        中途介入换掉 goal 是对的（当时的目标本来就没做到）；终局之后不行 ——
        原目标已经满足了，换成「再补一节用法」的话，下一轮看到的任务就只剩
        那半句，它会重新理解一遍甚至推翻已经做对的部分。
        """
        orch, _ = self._completed()
        original = orch.spec.goal

        orch.follow_up("再补一节「用法」")

        self.assertIn(original, orch.spec.goal, "原来的目标要留着")
        self.assertIn("再补一节「用法」", orch.spec.goal, "追加的那句也要在")
        self.assertIn("第 2 版", orch.spec.goal, "改过几轮之后要看得出哪句是这一轮的")

    def test_the_preempt_queue_is_drained(self):
        """**抢占队列必须清空**（同那条老坑）。

        `intervene` 会把信号塞进抢占队列，而追加要求这条路上没有任何 step 循环
        会来取它 —— 留着的话续跑的第一步刚开头就被它抢占一次，白烧一轮架构师。
        """
        orch, backend = self._completed()
        orch.follow_up("再补一节「用法」")

        self.assertIsNone(orch.bus.take_preempt(), "队列里不该还留着东西")
        # 一次追加 = 架构师被问一次。多问的那次就是被残留信号抢占出来的
        self.assertEqual(
            orch.state.interrupt_count, 1, "只该有人这一次中断"
        )

    def test_a_running_task_is_refused(self):
        """还在跑的用 intervene，挂起的用 ruling —— 三条路各管各的。"""
        orch, _ = self._completed()
        orch.state.status = TaskStatus.RUNNING

        with self.assertRaises(ValueError) as caught:
            orch.follow_up("再改改")
        self.assertIn("intervene", str(caught.exception))

    def test_restore_then_follow_up(self):
        """真实路径：进程早就退了，从存储把现场重建出来再续跑。"""
        orch, backend = self._completed()
        task_id = orch.spec.id

        revived = Orchestrator.restore(
            task_id, backend=backend, store=self.store, log=QUIET
        )
        result = revived.follow_up("再补一节「用法」")

        self.assertIs(result.state.status, TaskStatus.COMPLETED)
        self.assertEqual(result.state.spec.revision, 2)
        self.assertTrue(
            revived.ctx.produced, "restore 出来的现场要带着上一轮的产物"
        )

    def test_a_task_that_is_still_running_cannot_be_restored(self):
        """RUNNING / INTERRUPTED 不许 restore —— 那等于同一个任务跑两遍。"""
        orch, backend = self._completed()
        state = self.store.load_task(orch.spec.id)
        state.status = TaskStatus.RUNNING
        self.store.save_task(state)

        with self.assertRaises(ValueError):
            Orchestrator.restore(
                orch.spec.id, backend=backend, store=self.store, log=QUIET
            )


class TestFollowUpIsVisible(FollowUpFixture):
    """人说的话和「它又动起来了」都要落在时间线上。"""

    def test_the_instruction_lands_as_a_human_bubble(self):
        orch, _ = self._completed()
        orch.follow_up("再补一节「用法」")

        events = self.store.events_for(orch.spec.id, 0)
        human = [e for e in events if e.kind == "human"]
        self.assertEqual([e.text for e in human], ["再补一节「用法」"])

    def test_status_goes_back_from_terminal(self):
        """**终局不再等于「永远不会再动」**，M6 契约因此改了。"""
        orch, _ = self._completed()
        orch.follow_up("再补一节「用法」")

        seq = [
            e.payload.get("status")
            for e in self.store.events_for(orch.spec.id, 0)
            if e.kind == "status"
        ]
        self.assertEqual(seq[0], "COMPLETED", "第一轮先收尾")
        self.assertEqual(seq[-1], "COMPLETED", "续跑之后又收一次尾")
        self.assertIn("INTERRUPTED", seq[1:], "中间要能看出它被人打断过")
