"""架构师的停止判断（M5a，§12 M5 / §11.9）。

M2 归因给这三条改动定了方向：架构师的失效形态**不是「规格拆错了」，
是「不知道该停」**——ESCALATE 类 25 次运行里主动 ABANDON 只有 3 次，
80% 靠 policy 里的计数器兜住；`e1_silent_failure`（架构师手上零证据）
五次运行全是 CONTINUE / REASSIGN / MODIFY_TASK 轮流试，一次 ABANDON 都没有。

所以这里钉的是「原地打转能不能被确定性地看出来」，不是「模型判断得对不对」。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.agent.architect import Architect, AutoApproveGate
from cowork.escalation import deterministic_escalation
from cowork.llm import ArchitectVerdict
from cowork.llm.scripted import ScriptedBackend
from cowork.orchestrator import Orchestrator
from cowork.policy import Policy
from cowork.signals import SignalSource, SignalType, fingerprint
from cowork.store import SqliteStore
from cowork.types import (
    Criterion,
    SandboxProfile,
    Signal,
    TaskClass,
    TaskSpec,
    TaskState,
)


def sig(kind: SignalType, evidence: str, task_id: str = "t1") -> Signal:
    return Signal(type=kind, task_id=task_id, source=SignalSource.RUNTIME,
                  raw_evidence=evidence)


class TestFingerprint(unittest.TestCase):
    def test_same_signal_same_evidence_same_fingerprint(self):
        a = sig(SignalType.TEST_FAILED, "FAIL: f('') -> True")
        b = sig(SignalType.TEST_FAILED, "FAIL: f('') -> True")
        self.assertEqual(fingerprint([a]), fingerprint([b]))

    def test_different_evidence_differs(self):
        a = sig(SignalType.TEST_FAILED, "FAIL: 第一个用例")
        b = sig(SignalType.TEST_FAILED, "FAIL: 第二个用例")
        self.assertNotEqual(fingerprint([a]), fingerprint([b]))

    def test_different_type_differs(self):
        a = sig(SignalType.TEST_FAILED, "same")
        b = sig(SignalType.TOOL_FAILURE, "same")
        self.assertNotEqual(fingerprint([a]), fingerprint([b]))

    def test_order_does_not_matter(self):
        a = sig(SignalType.TEST_FAILED, "x")
        b = sig(SignalType.TOOL_FAILURE, "y")
        self.assertEqual(fingerprint([a, b]), fingerprint([b, a]))

    def test_evidence_is_hashed_not_embedded(self):
        """指纹要进日志和 DecisionRecord，不能把第三方错误体原文带进去。"""
        secret = "sk-should-not-appear-anywhere"
        self.assertNotIn(secret, fingerprint([sig(SignalType.TOOL_FAILURE, secret)]))


class TestStallRule(unittest.TestCase):
    """§7.2 新增的「决策无效」判据。"""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.spec = TaskSpec(
            id="t1", parent_id="parent", goal="做点什么",
            acceptance=[Criterion("c1", "做完")], task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws)), scope=["out.py"],
        )
        self.state = TaskState(spec=self.spec)
        self.verdict = ArchitectVerdict(action="CONTINUE", rationale="再试一次",
                                        complexity_score=0.1)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _reason(self, streak: int) -> str | None:
        return deterministic_escalation(
            Policy(), self.spec, self.state, [], self.verdict, identical_streak=streak
        )

    def test_first_occurrence_does_not_escalate(self):
        self.assertIsNone(self._reason(1))

    def test_second_identical_interrupt_escalates(self):
        reason = self._reason(2)
        self.assertIsNotNone(reason)
        self.assertIn("指纹完全相同", reason)

    def test_fires_before_max_interrupts(self):
        """区分「试了三次不同的办法」和「同一个办法试了三次」。

        interrupt_count 只会数到 3 才拦，而同一个办法第二次就该停了。
        """
        p = Policy()
        self.state.interrupt_count = 1  # 远未到 max_interrupts=3
        reason = deterministic_escalation(
            p, self.spec, self.state, [], self.verdict, identical_streak=2
        )
        self.assertIn("指纹完全相同", reason)

    def test_abandon_always_escalates(self):
        """放弃对该任务不可逆 —— 按 §7.2 第 1 条同理，该由人拍板。

        这条在 AutoApproveGate 下看不出效果（网关直接采纳裁决），
        它改变的是真实介入配置下的行为。
        """
        verdict = ArchitectVerdict(action="ABANDON", rationale="没救了",
                                   complexity_score=0.1)
        reason = deterministic_escalation(Policy(), self.spec, self.state, [], verdict)
        self.assertIsNotNone(reason)
        self.assertIn("不可逆", reason)

    def test_abandon_escalation_is_configurable(self):
        verdict = ArchitectVerdict(action="ABANDON", rationale="没救了",
                                   complexity_score=0.1)
        p = Policy(escalate_on_abandon=False)
        self.assertIsNone(
            deterministic_escalation(p, self.spec, self.state, [], verdict)
        )

    def test_configurable(self):
        p = Policy(max_identical_interrupts=5)
        self.assertIsNone(
            deterministic_escalation(p, self.spec, self.state, [], self.verdict,
                                     identical_streak=3)
        )


class TestArchitectRemembers(unittest.TestCase):
    """架构师记住自己试过什么 —— 这份记录同时喂确定性判据和提示词。"""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.store = SqliteStore()
        self.spec = TaskSpec(
            id="t1", parent_id="parent", goal="做点什么",
            acceptance=[Criterion("c1", "做完")], task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws)), scope=["out.py"],
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _architect(self, backend) -> Architect:
        return Architect(backend, self.store, policy=Policy(),
                         human_gate=AutoApproveGate())

    def test_history_reaches_the_backend(self):
        backend = ScriptedBackend(
            {},
            verdict_for=lambda s, sg: ArchitectVerdict(
                action="CONTINUE", rationale="再试", complexity_score=0.1
            ),
        )
        arch = self._architect(backend)
        state = TaskState(spec=self.spec)

        arch.decide(state, [sig(SignalType.TEST_FAILED, "第一次")], _ctx(self.spec))
        self.assertEqual(backend.decide_history, [], "第一次不该有历史")

        arch.decide(state, [sig(SignalType.TEST_FAILED, "第二次")], _ctx(self.spec))
        self.assertEqual(len(backend.decide_history), 1)
        self.assertEqual(backend.decide_history[0]["action"], "CONTINUE")

    def test_repeated_identical_interrupt_escalates(self):
        backend = ScriptedBackend(
            {},
            verdict_for=lambda s, sg: ArchitectVerdict(
                action="CONTINUE", rationale="再试一次", complexity_score=0.1
            ),
        )
        arch = self._architect(backend)
        state = TaskState(spec=self.spec)

        first = arch.decide(state, [sig(SignalType.TEST_FAILED, "同一个错")], _ctx(self.spec))
        self.assertIsNone(first.escalation_reason, "第一次不该升级")

        second = arch.decide(state, [sig(SignalType.TEST_FAILED, "同一个错")], _ctx(self.spec))
        self.assertIsNotNone(second.escalation_reason)
        self.assertIn("指纹完全相同", second.escalation_reason)

    def test_different_failures_do_not_escalate(self):
        """换了个错误就说明上一次决策起作用了，不该拦。"""
        backend = ScriptedBackend(
            {},
            verdict_for=lambda s, sg: ArchitectVerdict(
                action="CONTINUE", rationale="再试", complexity_score=0.1
            ),
        )
        arch = self._architect(backend)
        state = TaskState(spec=self.spec)

        arch.decide(state, [sig(SignalType.TEST_FAILED, "错误 A")], _ctx(self.spec))
        second = arch.decide(state, [sig(SignalType.TEST_FAILED, "错误 B")], _ctx(self.spec))
        self.assertIsNone(second.escalation_reason)


class TestStallEndToEnd(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        # 一个永远失败、且每次失败方式完全相同的任务
        (self.ws / "verify.py").write_text(
            "import sys\nprint('FAIL: 永远过不了', file=sys.stderr)\nsys.exit(1)\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_hopeless_task_stops_early(self):
        """反复撞同一堵墙时，停下来的依据是确定性的，不用问模型。"""
        spec = TaskSpec(
            id="t1", parent_id="parent",
            goal="让 verify.py 通过（不可能）",
            acceptance=[Criterion("c1", "过", ["python", "verify.py"])],
            task_class=TaskClass.CODE,
            output_schema={},
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=["out.py"], max_steps=4,
        )
        steps = {}
        for rev in range(1, 9):
            steps[(rev, 0)] = ToolCall("write_file", {"path": "out.py", "content": "x=1"})
            steps[(rev, 1)] = Finish(output={}, summary="自认为好了")
        backend = ScriptedBackend(
            steps,
            verdict_for=lambda s, sg: ArchitectVerdict(
                action="CONTINUE", rationale="再试一次", complexity_score=0.1
            ),
        )
        orch = Orchestrator(spec, backend=backend, store=SqliteStore(),
                            human_gate=None, log=lambda _m: None)
        result = orch.run(max_cycles=8)

        reasons = [d.escalation_reason for d in result.decisions if d.escalation_reason]
        self.assertTrue(any("指纹完全相同" in r for r in reasons), reasons)
        # 没有 human_gate -> 升级即挂起。关键是它在跑满 8 轮之前就停了
        self.assertLessEqual(result.state.interrupt_count, 3)


def _ctx(spec):
    from cowork.types import AgentContext

    return AgentContext(task_spec=spec)


if __name__ == "__main__":
    unittest.main()
