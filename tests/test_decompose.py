"""拆解生成侧与生成-复核循环（§12 M7 7.3 / 7.4 / 7.5）。

**这批测试验证不了「模型拆得好不好」** —— 那要真实模型，见 §11.12。
这里钉的是四件确定性的事：

  补全边界   模型只填 goal / 验收 / scope / 依赖，沙箱与工具白名单它碰不到
  循环形状   生成 → 复核 → 重生成 ≤N → 升级给人，且复核意见真的喂回去了
  写权归属   复核者和人都改不了 spec，重新拆的动作永远由生成者做
  三种终局   ACCEPTED / AWAITING_HUMAN / REJECTED 都不是异常

循环的判据故意复用 `escalation.deterministic_plan_escalation` 而不是新写一套 ——
§12 M7 明写「发现自己在写平行逻辑就是方向错了」，这里也顺带钉住这一点。
"""

from __future__ import annotations

import tempfile
import unittest

from cowork.agent.architect import (
    Architect,
    AutoApproveGate,
    DecompositionResult,
    PlanRuling,
    SpecTemplate,
)
from cowork.escalation import deterministic_plan_escalation
from cowork.llm import SubtaskDraft
from cowork.llm.errors import ModelCallFailed
from cowork.llm.scripted import ScriptedBackend
from cowork.policy import Policy
from cowork.store import SqliteStore
from cowork.types import SandboxProfile, TaskClass

GOAL = "做一个把文本行渲染成报告的小工具：解析、格式化、组装，最后整体校验一遍。"


def draft(tid: str, *, deps=(), scope=None, cls="CODE", criteria=None) -> SubtaskDraft:
    return SubtaskDraft(
        id=tid,
        goal=f"{tid} 要做的事",
        acceptance=criteria or [{"id": "c1", "description": f"{tid} 的行为判据",
                                 "command": ["python", f"verify_{tid}.py"]}],
        scope=list(scope or [f"{tid}.py"]),
        depends_on=list(deps),
        task_class=cls,
    )


GOOD = [draft("t1"), draft("t2"), draft("t3", deps=("t1", "t2"))]


def template(**over) -> SpecTemplate:
    base = dict(sandbox=SandboxProfile(workspace=tempfile.mkdtemp()), parent_id="root")
    base.update(over)
    return SpecTemplate(**base)


class TestAssembly(unittest.TestCase):
    """`SubtaskDraft` → `TaskSpec` 的补全（7.3）。"""

    def setUp(self):
        self.arch = Architect(
            ScriptedBackend({}, decompose_for=lambda g, f: GOOD),
            SqliteStore(), policy=Policy(),
        )

    def test_model_fields_survive(self):
        specs = self.arch.decompose(GOAL, template())
        self.assertEqual([s.id for s in specs], ["t1", "t2", "t3"])
        self.assertEqual(specs[2].depends_on, ["t1", "t2"])
        self.assertEqual(specs[0].scope, ["t1.py"])
        self.assertTrue(specs[0].acceptance[0].machine_checkable)

    def test_sandbox_and_tools_come_from_the_template_only(self):
        """模型不该有权给自己配隔离边界 —— 拆解 JSON 里压根没有这些字段。"""
        tpl = template(tools=("read_file",), token_budget=123, max_steps=4)
        specs = self.arch.decompose(GOAL, tpl)
        for s in specs:
            self.assertEqual(s.sandbox, tpl.sandbox)
            self.assertEqual(s.tools, ["read_file"])
            self.assertEqual(s.token_budget, 123)
            self.assertEqual(s.max_steps, 4)
            self.assertEqual(s.parent_id, "root")

    def test_generative_subtask_gets_a_probe_interval(self):
        """GENERATIVE 强制 PROBE，而 PROBE 缺间隔时 TaskSpec 直接拒收（§4.1）。"""
        backend = ScriptedBackend({}, decompose_for=lambda g, f: [draft("w1", cls="GENERATIVE")])
        arch = Architect(backend, SqliteStore(), policy=Policy())

        spec = arch.decompose(GOAL, template())[0]

        self.assertIs(spec.task_class, TaskClass.GENERATIVE)
        self.assertEqual(spec.probe_interval_s, Policy().default_probe_interval_s)

    def test_criterion_without_command_stays_human_judged(self):
        backend = ScriptedBackend({}, decompose_for=lambda g, f: [
            draft("t1", criteria=[{"id": "c1", "description": "读起来通顺", "command": None}])
        ])
        arch = Architect(backend, SqliteStore(), policy=Policy())
        spec = arch.decompose(GOAL, template())[0]
        self.assertFalse(spec.acceptance[0].machine_checkable)


class TestPlanLoop(unittest.TestCase):
    """生成 → 复核 → 重生成 ≤N → 升级给人（7.4）。"""

    def _arch(self, *, decompose_for, review_for, policy=None, gate=None) -> Architect:
        return Architect(
            ScriptedBackend({}, decompose_for=decompose_for, review_for=review_for),
            SqliteStore(), policy=policy or Policy(), human_gate=gate,
        )

    def test_subtasks_get_a_shared_parent(self):
        """没有共同 parent_id 时三处都坏（执行层升级、界面折叠、复合时间线）。

        实测撞到过：4 个子任务里 2 个第一次要改规格就挂起了 ——
        parent_id 为空 = 顶层任务，任何 MODIFY_TASK 都无条件升级（§7.2 第 3 条）。
        """
        arch = self._arch(decompose_for=lambda g, f: GOOD,
                          review_for=lambda g, s: (True, []))
        result = arch.plan(GOAL, template())

        roots = {s.parent_id for s in result.specs}
        self.assertEqual(len(roots), 1, "所有子任务必须同属一个根")
        self.assertIsNotNone(roots.pop())
        self.assertEqual(result.root_id, result.specs[0].parent_id)
        self.assertIn("root_id", result.to_dict())

    def test_explicit_parent_is_respected(self):
        arch = self._arch(decompose_for=lambda g, f: GOOD,
                          review_for=lambda g, s: (True, []))
        result = arch.plan(GOAL, template(parent_id="task_我自己给的"))
        self.assertEqual(result.root_id, "task_我自己给的")

    def test_clean_first_try_accepts(self):
        arch = self._arch(decompose_for=lambda g, f: GOOD,
                          review_for=lambda g, s: (True, []))
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.decider, "LLM")
        self.assertTrue(result.review.clean)
        self.assertGreater(result.tokens, 0)

    def test_findings_are_fed_back_into_the_regeneration(self):
        """没有这一步，重生成就只是「再抽一次」（同 §11.9b 的教训）。"""
        seen: list[list[str] | None] = []

        def decompose_for(goal, feedback):
            seen.append(feedback)
            return GOOD if feedback else [draft("t1"), draft("t2")]

        calls = {"n": 0}

        def review_for(goal, specs):
            calls["n"] += 1
            return (True, []) if calls["n"] > 1 else (False, ["没有人负责组装"])

        arch = self._arch(decompose_for=decompose_for, review_for=review_for)
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(seen[0], None, "第一轮不该有 feedback")
        self.assertEqual(seen[1], ["没有人负责组装"], "复核缺口必须原样喂回生成者")

    def test_structural_issues_also_become_feedback(self):
        """结构那一半是免费的，更该喂回去 —— 它连 token 都不用花就说清了问题。"""
        seen: list[list[str] | None] = []

        def decompose_for(goal, feedback):
            seen.append(feedback)
            # 第一轮拆成一条链：没有并行度，结构检查的 fan_out 会叫
            return GOOD if feedback else [draft("a"), draft("b", deps=("a",))]

        arch = self._arch(decompose_for=decompose_for, review_for=lambda g, s: (True, []))
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "ACCEPTED")
        self.assertTrue(any("fan_out" in x for x in seen[1]))

    def test_repeated_identical_findings_escalate_early(self):
        """复核意见一字不变地又来一遍 = 重生成没有改变现实（§7.2 第 1b 条同源）。"""
        arch = self._arch(
            decompose_for=lambda g, f: [draft("t1"), draft("t2")],
            review_for=lambda g, s: (False, ["永远缺同一个东西"]),
            policy=Policy(max_regenerate=5),
        )
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertEqual(result.attempts, 2, "第二轮就该停，不该跑满 max_regenerate")
        self.assertIn("重生成没有改变现实", result.escalation_reason)

    def test_regeneration_cap_escalates(self):
        """每轮都报不同的缺口时，靠上限兜住。"""
        n = {"i": 0}

        def review_for(goal, specs):
            n["i"] += 1
            return False, [f"第 {n['i']} 个不同的缺口"]

        arch = self._arch(decompose_for=lambda g, f: [draft("t1"), draft("t2")],
                          review_for=review_for, policy=Policy(max_regenerate=2))
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertEqual(result.attempts, 3, "1 次初拆 + 2 次重生成")
        self.assertIn("max_regenerate", result.escalation_reason)

    def test_model_failure_becomes_a_terminal_state_not_an_exception(self):
        """架构师连拆解都拆不出来时不该猜一个 —— 和执行层同一条路。"""
        def boom(goal, feedback):
            raise ModelCallFailed("供应商 500")

        arch = self._arch(decompose_for=boom, review_for=lambda g, s: (True, []))
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertIn("供应商 500", result.escalation_reason)
        self.assertEqual(result.specs, [])

    def test_reviewer_failure_takes_the_same_path_as_generator_failure(self):
        """手上有拆解、只是没人复核得了 —— 该交给人，不该抛穿（§11.13 实测撞到）。"""
        def boom(goal, specs):
            raise ModelCallFailed("复核者被截断")

        arch = self._arch(decompose_for=lambda g, f: GOOD, review_for=boom)
        result = arch.plan(GOAL, template())

        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertIn("复核者无法给出结论", result.escalation_reason)
        self.assertEqual([s.id for s in result.specs], [s.id for s in GOOD],
                         "拆解还在手上，不该丢掉")

    def test_history_records_every_round(self):
        n = {"i": 0}

        def review_for(goal, specs):
            n["i"] += 1
            return (True, []) if n["i"] > 1 else (False, ["缺了点什么"])

        arch = self._arch(decompose_for=lambda g, f: GOOD, review_for=review_for)
        result = arch.plan(GOAL, template())

        self.assertEqual([h["attempt"] for h in result.history], [1, 2])
        self.assertEqual(result.history[0]["missing"], ["缺了点什么"])
        self.assertTrue(result.history[1]["clean"])
        self.assertIn("fingerprint", result.history[0])


class TestHumanEntry(unittest.TestCase):
    """拆解层的人的入口（7.5）。人是仲裁者，不是第二个写入点。"""

    def _arch(self, gate) -> Architect:
        return Architect(
            ScriptedBackend({}, decompose_for=lambda g, f: [draft("t1"), draft("t2")],
                            review_for=lambda g, s: (False, ["总是缺同一样"])),
            SqliteStore(), policy=Policy(), human_gate=gate,
        )

    def test_no_plan_entry_means_awaiting_human(self):
        """有中断网关但没有拆解入口时也不猜 —— 挂起。"""
        class OnlyInterrupts:
            def review(self, spec, signals, verdict, reason):
                raise AssertionError("拆解不该走中断网关")

        result = self._arch(OnlyInterrupts()).plan(GOAL, template())
        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertIn("没有拆解层的介入入口", result.rationale)

    def test_auto_approve_gate_accepts_the_current_plan(self):
        result = self._arch(AutoApproveGate()).plan(GOAL, template())
        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual(result.decider, "HUMAN")
        self.assertIsNotNone(result.escalation_reason, "自动放行不该抹掉升级原因")

    def test_human_can_reject_the_goal(self):
        class Reject:
            def review_plan(self, root_goal, specs, review, reason):
                return PlanRuling(accept=False, rationale="这个目标先不做")

        result = self._arch(Reject()).plan(GOAL, template())
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.rationale, "这个目标先不做")

    def test_human_can_hand_in_their_own_decomposition(self):
        """人有写权（§2.4），这是它在拆解层的体现。"""
        arch = self._arch(None)
        mine = arch.decompose(GOAL, template())

        class Override:
            def review_plan(self, root_goal, specs, review, reason):
                return PlanRuling(accept=True, rationale="我自己拆了一份", specs=mine)

        result = self._arch(Override()).plan(GOAL, template())
        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual([s.id for s in result.specs], [s.id for s in mine])

    def test_no_answer_keeps_it_pending(self):
        class Silent:
            def review_plan(self, root_goal, specs, review, reason):
                return None

        result = self._arch(Silent()).plan(GOAL, template())
        self.assertEqual(result.status, "AWAITING_HUMAN")
        self.assertEqual(result.decider, "HUMAN")


class TestWritePathOwnership(unittest.TestCase):
    """复核者是顾问，不是第二个写入点（§2.3）。"""

    def test_reviewer_never_produces_specs(self):
        reviewer = ScriptedBackend({}, review_for=lambda g, s: (False, ["缺了组装"]))
        generator = ScriptedBackend(
            {}, decompose_for=lambda g, f: GOOD, review_for=lambda g, s: (True, []))
        arch = Architect(generator, SqliteStore(), policy=Policy(),
                         reviewer_backend=reviewer, human_gate=AutoApproveGate())

        result = arch.plan(GOAL, template())

        # 复核者被问了，但产出 spec 的始终是生成者
        self.assertGreater(reviewer.review_calls, 0)
        self.assertEqual(generator.review_calls, 0)
        self.assertEqual(reviewer.decompose_calls, 0, "复核者不该被要求拆解")
        self.assertEqual([s.id for s in result.specs], [s.id for s in GOOD])


class TestPlanEscalationRule(unittest.TestCase):
    """确定性判据本身。它和执行层那套共用 policy，不是平行实现。"""

    def test_no_escalation_on_first_failure(self):
        self.assertIsNone(
            deterministic_plan_escalation(Policy(), attempt=1, fingerprints=["a"])
        )

    def test_identical_findings_twice(self):
        reason = deterministic_plan_escalation(Policy(), attempt=2, fingerprints=["a", "a"])
        self.assertIn("重生成没有改变现实", reason)

    def test_different_findings_do_not_trigger_the_streak_rule(self):
        self.assertIsNone(
            deterministic_plan_escalation(
                Policy(max_regenerate=5), attempt=2, fingerprints=["a", "b"]
            )
        )

    def test_cap_uses_max_regenerate(self):
        reason = deterministic_plan_escalation(
            Policy(max_regenerate=2), attempt=3, fingerprints=["a", "b", "c"]
        )
        self.assertIn("已重生成 2 次", reason)

    def test_result_serializes(self):
        r = DecompositionResult(status="ACCEPTED", specs=[])
        self.assertEqual(r.to_dict()["status"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
