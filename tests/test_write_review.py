"""写入侧复核（§12 M8）：改 TaskSpec 之前先让复核者看一眼。

它补的是风险 #3 剩下的那块暴露面 —— M2 实测 176 条裁决里有 34 条（19%）
**改了 spec 而且确定性规则没有把它送到人面前**。

这一层与 `escalation.py` 分工不重叠：确定性判据看的是**上下文**
（谁改的、改过几次、烧了多少钱），从不看改动内容；这里看的正是内容。

循环与拆解层同构，判据同样来自 policy.max_regenerate：
    拆解层：生成 → 复核 → 重生成 ≤N → 升级给人
    写入侧：决策 → 复核 → 重做   ≤N → 升级给人
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.agent.architect import Architect
from cowork.llm import ArchitectVerdict
from cowork.llm.scripted import ScriptedBackend
from cowork.policy import DEFAULT_POLICY
from cowork.runtime.bus import SignalBus
from cowork.signals import SignalType
from cowork.store import SqliteStore
from cowork.types import (
    Action,
    AgentContext,
    Criterion,
    Decider,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskState,
)

ADD_CRITERION = {
    "added_criteria": [{"id": "c2", "description": "输入为空串时返回 False"}]
}
LOOSEN_GOAL = {"goal": "实现 is_palindrome，能跑就行"}


def _verdict(changes, rationale="补一条验收标准") -> ArchitectVerdict:
    return ArchitectVerdict(
        action="MODIFY_TASK",
        rationale=rationale,
        complexity_score=0.1,  # 低于 complexity_threshold，不会因自评被升级
        spec_changes=changes,
    )


class WriteReviewFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-wr-"))
        self.store = SqliteStore()
        self.bus = SignalBus()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def spec(self) -> TaskSpec:
        return TaskSpec(
            goal="实现 is_palindrome(s) -> bool",
            parent_id="task_parent",  # 顶层 MODIFY_TASK 会被确定性规则直接升级
            acceptance=[Criterion("c1", "verify.py 通过", ["python", "verify.py"])],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=["solution.py"],
        )

    def signals(self, spec):
        sig = self.bus.emit_hard(
            SignalType.TEST_FAILED, spec.id,
            evidence="FAIL: is_palindrome('') -> True, expected False",
        )
        self.store.save_signal(sig)
        return [sig]

    def architect(self, backend, *, reviewer=None, review_writes=True) -> Architect:
        return Architect(
            backend, self.store, policy=DEFAULT_POLICY,
            reviewer_backend=reviewer, review_writes=review_writes,
        )

    def decide(self, arch, spec, signals):
        state = TaskState(spec=spec)
        self.store.save_task(state)
        return arch.decide(state, signals, AgentContext(task_spec=spec))


class TestReviewGate(WriteReviewFixture):
    def test_on_by_default(self):
        """默认开（§11.19：26 用例两臂，J 0.963 / 0.907，FPR 都是 0/24）。

        它防的是六种缺陷里**没有兜底**的那两种（目标被改松、scope 扩到校验脚本）
        —— 那两种会让任务假装成功，没有任何后续信号会暴露。
        """
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (True, []))
        arch = Architect(  # 不传 review_writes
            backend, self.store, policy=DEFAULT_POLICY, reviewer_backend=reviewer
        )

        self.decide(arch, spec, self.signals(spec))

        self.assertEqual(reviewer.spec_review_calls, 1, "默认就该调复核者")

    def test_can_be_turned_off(self):
        """界面设置页要能关掉它 —— 判别力测过，但重做循环没在真实链路上跑过。"""
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, ["随便报一条"]))
        arch = self.architect(backend, reviewer=reviewer, review_writes=False)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(reviewer.spec_review_calls, 0)
        self.assertIs(rec.action, Action.MODIFY_TASK)
        self.assertIsNone(rec.escalation_reason)

    def test_backend_without_the_capability_degrades_instead_of_crashing(self):
        """**默认开之后，这条才是最容易伤到别人的地方。**

        `review_spec_change` 是 M8 才进 Backend 协议的。开关默认开 = 任何早于 M8
        的后端实现（包括别人写的）都会在这里 AttributeError。没有复核者时的正确
        行为是回到 M8 之前的样子（架构师自己拍板），不是把整条链打挂。
        """

        class OldBackend(ScriptedBackend):
            review_spec_change = None

            def __getattribute__(self, name):
                if name == "review_spec_change":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        spec = self.spec()
        backend = OldBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))
        arch = self.architect(backend, reviewer=None)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertIs(rec.action, Action.MODIFY_TASK, "该照常改 spec")
        self.assertIsNone(rec.escalation_reason, "不该因为后端老就升级给人")
        self.assertIn("c2", [c.id for c in rec.new_spec.acceptance])

    def test_bench_pins_it_off(self):
        """跑批必须显式关掉：M2/M3 的参数全部是在没有写入侧复核时测出来的，
        开着它跑批，新数据和 `bench_runs.jsonl` 就不可比了。
        """
        import inspect

        from cowork.bench import runner as bench_runner

        src = inspect.getsource(bench_runner.run_once)
        self.assertIn("review_writes=False", src, "bench 没有把写入侧复核钉死为关")

    def test_approved_change_passes_through_untouched(self):
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (True, []))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(reviewer.spec_review_calls, 1)
        self.assertIs(rec.action, Action.MODIFY_TASK)
        self.assertIsNone(rec.escalation_reason, "复核通过不该升级")
        self.assertIn("c2", [c.id for c in rec.new_spec.acceptance])

    def test_only_writes_are_reviewed(self):
        """CONTINUE / REASSIGN 不改 spec，不构成风险 #3，不该花这次调用。"""
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: ArchitectVerdict(
            action="CONTINUE", rationale="小修", complexity_score=0.1))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, ["不该被问到"]))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(reviewer.spec_review_calls, 0)
        self.assertIs(rec.action, Action.CONTINUE)

    def test_already_escalating_is_not_reviewed(self):
        """确定性规则已经要把它送到人面前了 —— 再花一次复核没有意义。"""
        spec = self.spec().bump(parent_id=None)  # 顶层任务：MODIFY_TASK 必升级
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (True, []))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(reviewer.spec_review_calls, 0)
        self.assertIsNotNone(rec.escalation_reason)


class TestRedoLoop(WriteReviewFixture):
    def _flip_flop_backend(self, first, second):
        """第一轮给 first，之后给 second —— 模拟「被驳回后改好了」。"""
        calls = {"n": 0}

        def verdict_for(*_):
            calls["n"] += 1
            return _verdict(first if calls["n"] == 1 else second)

        return ScriptedBackend({}, verdict_for=verdict_for), calls

    def test_findings_are_fed_back_into_the_redo(self):
        """不喂回去的话架构师会在「第一次看到这个中断」的状态下重做，
        复核意见等于没提（§11.9b 的教训，和 decompose(feedback=) 同一个设计）。
        """
        spec = self.spec()
        backend, _ = self._flip_flop_backend(LOOSEN_GOAL, ADD_CRITERION)
        seen = {"n": 0}

        def review(*_):
            seen["n"] += 1
            return (True, []) if seen["n"] > 1 else (False, ["把目标改松了"])

        reviewer = ScriptedBackend({}, spec_review_for=review)
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(backend.decide_feedback[0], None, "第一轮没有复核意见")
        self.assertEqual(backend.decide_feedback[1], ["把目标改松了"], "第二轮要带着意见")
        self.assertIs(rec.action, Action.MODIFY_TASK)
        self.assertIsNone(rec.escalation_reason, "改好了就该放行")

    def test_persistent_rejection_escalates_to_human(self):
        """重做用完 max_regenerate 还不过 —— 交给人，不是硬来也不是放弃。"""
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(LOOSEN_GOAL))
        reviewer = ScriptedBackend(
            {}, spec_review_for=lambda *_: (False, ["把目标改松了：原目标要求处理空串"]))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        # 复核 N+1 次（初版 + 每次重做后各一次）
        self.assertEqual(reviewer.spec_review_calls, DEFAULT_POLICY.max_regenerate + 1)
        self.assertIsNotNone(rec.escalation_reason)
        self.assertIn("复核者连续", rec.escalation_reason)
        self.assertIn("把目标改松了", rec.escalation_reason)
        # 没有网关 -> 挂起，且模型的建议要留痕（M6 §9）
        self.assertIsNone(rec.new_spec, "挂起时不该已经把 spec 改掉")
        self.assertIsNotNone(rec.suggestion)
        self.assertEqual(rec.suggestion["spec_changes"], LOOSEN_GOAL)

    def test_reviewer_has_no_write_power(self):
        """复核者只回 findings，改不了 spec —— 写权仍然只在 decide() 这一条路上。

        给它写权 = 两个写入点 = §2.3 的不变量破了。这条用断言钉住：
        复核者说什么都不会出现在最终 spec 里。
        """
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))
        reviewer = ScriptedBackend(
            {}, spec_review_for=lambda *_: (True, ["顺便把 scope 扩到 **/*.py"]))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(rec.new_spec.scope, ["solution.py"], "复核者动不了 scope")
        self.assertEqual(rec.spec_changes, ADD_CRITERION)

    def test_redone_verdict_is_re_checked_against_the_deterministic_floor(self):
        """**重做出来的是一份新裁决，必须重新过一遍确定性下限。**

        复核循环开始前那次 `should_escalate` 判的是第一版；复核驳回之后架构师
        改判 ABANDON，而 §7.2 明写「任何 ABANDON 都不可逆、要人确认」。
        不重判的话，「让复核者看一眼」反而成了绕过升级下限的通道 ——
        审计实测：架构师被驳回后改判 ABANDON，人一次都没有被问到。
        """
        spec = self.spec()
        calls = {"n": 0}

        def verdict_for(*_):
            calls["n"] += 1
            if calls["n"] == 1:
                return _verdict(LOOSEN_GOAL)
            return ArchitectVerdict(action="ABANDON", rationale="复核说不行，那就放弃",
                                    complexity_score=0.1)

        backend = ScriptedBackend({}, verdict_for=verdict_for)
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, ["目标被改松"]))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertIsNotNone(rec.escalation_reason, "ABANDON 必须升级给人")
        self.assertIn("ABANDON", rec.escalation_reason)
        self.assertIsNot(rec.action, Action.ABANDON,
                         "没有网关时该挂起等人，不是直接放弃")

    def test_redone_verdict_with_high_complexity_is_re_checked(self):
        """同一条路的另一半：重做把自评复杂度抬过了阈值，也要重判。"""
        spec = self.spec()
        calls = {"n": 0}

        def verdict_for(*_):
            calls["n"] += 1
            score = 0.1 if calls["n"] == 1 else 0.95
            return ArchitectVerdict(action="MODIFY_TASK", rationale="改法二",
                                    complexity_score=score,
                                    spec_changes=ADD_CRITERION)

        backend = ScriptedBackend({}, verdict_for=verdict_for)
        seen = {"n": 0}

        def review(*_):
            seen["n"] += 1
            return (True, []) if seen["n"] > 1 else (False, ["第一版不行"])

        reviewer = ScriptedBackend({}, spec_review_for=review)
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertIsNotNone(rec.escalation_reason)
        self.assertIn("complexity_score", rec.escalation_reason)

    def test_review_findings_are_kept_on_the_record(self):
        """复核者说了什么要跟着裁决走，不能只有前三条拼进升级原因。"""
        spec = self.spec()
        findings = [f"第 {i} 条毛病" for i in range(1, 6)]
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(LOOSEN_GOAL))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, findings))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(rec.suggestion["review_findings"], findings,
                         "第四条起原来当场丢失")

    def test_redo_that_stops_writing_leaves_the_loop(self):
        """重做之后不改 spec 了 —— 没有复核对象，回主路径按常规判。"""
        spec = self.spec()
        calls = {"n": 0}

        def verdict_for(*_):
            calls["n"] += 1
            if calls["n"] == 1:
                return _verdict(LOOSEN_GOAL)
            return ArchitectVerdict(action="CONTINUE", rationale="改不动，先继续",
                                    complexity_score=0.1)

        backend = ScriptedBackend({}, verdict_for=verdict_for)
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, ["目标被改松"]))
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertIs(rec.action, Action.CONTINUE)
        self.assertIsNone(rec.escalation_reason)
        self.assertEqual(reviewer.spec_review_calls, 1, "不再是写入就不该继续复核")


class TestBothSidesFailTheSameWay(WriteReviewFixture):
    """「A 失败有兜底」的地方都要问 B 失败走哪儿（§11.13 踩过）。"""

    def test_reviewer_model_failure_escalates_instead_of_crashing(self):
        from cowork.llm.errors import ModelError

        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(ADD_CRITERION))

        def boom(*_):
            raise ModelError("复核者的 key 没余额了")

        reviewer = ScriptedBackend({}, spec_review_for=boom)
        arch = self.architect(backend, reviewer=reviewer)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertIsNotNone(rec.escalation_reason)
        self.assertIn("复核者无法给出结论", rec.escalation_reason)
        self.assertIsNone(rec.new_spec, "没人复核得了就交给人，不是硬改下去")

    def test_same_model_review_when_no_independent_reviewer(self):
        """没给独立复核者时退回同模型复核（M5b 的形态）—— 弱，但不是没有。"""
        spec = self.spec()
        backend = ScriptedBackend(
            {},
            verdict_for=lambda *_: _verdict(ADD_CRITERION),
            spec_review_for=lambda *_: (True, []),
        )
        arch = self.architect(backend, reviewer=None)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertEqual(backend.spec_review_calls, 1)
        self.assertIs(rec.action, Action.MODIFY_TASK)

    def test_same_model_gets_only_one_redo(self):
        """同模型复核时重做压到一轮。

        跨模型驳回能带来生成者没有的信息，重做有价值；同模型驳回后重做是
        **同一套先验再试一次** —— M7 实测重生成收益本就在衰减（62% → 33%），
        共享盲点只会衰减得更快。省下那一轮直接交给人更划算。
        """
        spec = self.spec()
        backend = ScriptedBackend(
            {},
            verdict_for=lambda *_: _verdict(LOOSEN_GOAL),
            spec_review_for=lambda *_: (False, ["把目标改松了"]),
        )
        arch = self.architect(backend, reviewer=None)

        rec = self.decide(arch, spec, self.signals(spec))

        # 同模型：初版 + 1 次重做后 = 2 次复核（跨模型是 max_regenerate + 1 = 3）
        self.assertEqual(backend.spec_review_calls, 2)
        self.assertIsNotNone(rec.escalation_reason)
        # 升级文案里的轮数要和实际轮数一致：原来写死 policy.max_regenerate + 1，
        # 同模型时会说成 3 轮 —— 人读着记录去复盘，数字对不上就没法查
        self.assertIn("连续 2 轮", rec.escalation_reason)

    def test_cross_model_gets_the_full_redo_budget(self):
        """对照上一条：给了独立复核者才用满 max_regenerate。"""
        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(LOOSEN_GOAL))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, ["改松了"]))
        arch = self.architect(backend, reviewer=reviewer)

        self.decide(arch, spec, self.signals(spec))

        self.assertEqual(reviewer.spec_review_calls, DEFAULT_POLICY.max_regenerate + 1)


class TestBudgetVisibility(WriteReviewFixture):
    """复核循环烧的 token 必须进 `state.tokens_used`。

    M8 首版只加到 `Architect.tokens_used`（架构师自己的账），而
    `escalation.py` 的 `budget_escalation_ratio` 读的是 `state.tokens_used` ——
    于是复核循环的开销对预算检查**完全隐形**：一个能反复重做的循环，
    却不算进「这个任务花了多少」。
    """

    def test_review_tokens_land_on_the_task(self):
        spec = self.spec()
        backend = ScriptedBackend(
            {}, verdict_for=lambda *_: _verdict(ADD_CRITERION), token_cost=1000
        )
        reviewer = ScriptedBackend(
            {}, spec_review_for=lambda *_: (True, []), token_cost=1000
        )
        arch = self.architect(backend, reviewer=reviewer)

        state = TaskState(spec=spec)
        self.store.save_task(state)
        before = state.tokens_used
        arch.decide(state, self.signals(spec), AgentContext(task_spec=spec))

        # decide_interrupt(1000) + review_spec_change(1000//5=200)
        self.assertEqual(state.tokens_used - before, 1200)

    def test_redo_rounds_also_count(self):
        """重做那几轮尤其要算 —— 它们正是这个循环最贵的部分。"""
        spec = self.spec()
        backend = ScriptedBackend(
            {}, verdict_for=lambda *_: _verdict(LOOSEN_GOAL), token_cost=1000
        )
        reviewer = ScriptedBackend(
            {}, spec_review_for=lambda *_: (False, ["改松了"]), token_cost=1000
        )
        arch = self.architect(backend, reviewer=reviewer)

        state = TaskState(spec=spec)
        self.store.save_task(state)
        arch.decide(state, self.signals(spec), AgentContext(task_spec=spec))

        # 初版决策 1000 + 复核 3 次 ×200 + 重做 2 次 ×1000 = 3600
        self.assertEqual(state.tokens_used, 3600)


class TestHumanArbitrates(WriteReviewFixture):
    def test_human_ruling_overrides_the_rejected_change(self):
        """复核驳回 → 升级 → 人拍板。人的答复仍然经架构师落地（§7 第 1 条）。"""
        from cowork.agent.architect import HumanRuling

        class Gate:
            def review(self, spec, signals, verdict, reason):
                return HumanRuling(
                    action=Action.MODIFY_TASK,
                    rationale="我来写这条标准",
                    spec_changes=ADD_CRITERION,
                )

        spec = self.spec()
        backend = ScriptedBackend({}, verdict_for=lambda *_: _verdict(LOOSEN_GOAL))
        reviewer = ScriptedBackend({}, spec_review_for=lambda *_: (False, ["目标被改松"]))
        arch = Architect(backend, self.store, policy=DEFAULT_POLICY,
                         human_gate=Gate(), reviewer_backend=reviewer, review_writes=True)

        rec = self.decide(arch, spec, self.signals(spec))

        self.assertIs(rec.decider, Decider.HUMAN)
        self.assertIn("c2", [c.id for c in rec.new_spec.acceptance])
        self.assertEqual(rec.new_spec.goal, spec.goal, "人没同意改目标，目标就不该变")
        # 模型当时想干什么要留痕，供事后复盘对照
        self.assertEqual(rec.suggestion["spec_changes"], LOOSEN_GOAL)


if __name__ == "__main__":
    unittest.main()
