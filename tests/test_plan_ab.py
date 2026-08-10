"""拆解提示词对照的用例表与指标（§12 M7 7.4 / 风险 #17）。

同 `test_review_ab`：这里验证不了「限定词纪律值多少」，那要真实模型（§11.13）。
钉的是三件会让结论失真的事：

  对照臂立不立得住   naive 必须仍然满足装配层的硬约束，否则测的是 schema 合规
  循环指标的口径     「被驳回 → 救回来」只能在真的被驳回的运行上算
  升级原因的归类     repeat / cap / model_failure 分错了，风险 #17 的结论就反了
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cowork.bench import plan_ab
from cowork.bench.plan_ab import (
    GOALS,
    NAIVE_DECOMPOSE_SYSTEM,
    PlanRecord,
    arm_summary,
    loop_evidence,
    plan_once,
    run_batch,
    select_goals,
    summarize,
)
from cowork.llm.anthropic_backend import DECOMPOSE_SYSTEM
from cowork.llm.scripted import ScriptedBackend
from cowork.llm import SubtaskDraft


class TestGoalTable(unittest.TestCase):
    def test_ids_unique_and_selectable(self):
        self.assertEqual(len({g.id for g in GOALS}), len(GOALS))
        self.assertEqual([g.id for g in select_goals("wc")], ["wc"])
        self.assertEqual(len(select_goals(None)), len(GOALS))

    def test_every_goal_carries_several_limiters(self):
        """限定词少的目标区分不出两个臂 —— 那正是 naive 也不会漏的情形。"""
        for g in GOALS:
            self.assertGreaterEqual(len(g.limiters), 3, g.id)

    def test_naive_arm_still_states_the_assembly_constraints(self):
        """对照臂不能是稻草人：scope 不相交 / 无环 / 有验收标准是装配层本来就拦的。"""
        for must in ("scope", "depends_on", "验收标准"):
            self.assertIn(must, NAIVE_DECOMPOSE_SYSTEM)

    def test_naive_arm_drops_exactly_the_discipline_under_test(self):
        self.assertIn("限定词", DECOMPOSE_SYSTEM)
        self.assertNotIn("限定词", NAIVE_DECOMPOSE_SYSTEM)
        self.assertNotIn("存在性", NAIVE_DECOMPOSE_SYSTEM)


def _drafts(n: int = 2):
    return [
        SubtaskDraft(id=f"t{i}", goal=f"做 t{i}",
                     acceptance=[{"id": "c1", "description": "行为判据", "command": None}],
                     scope=[f"t{i}.py"], depends_on=[], task_class="CODE")
        for i in range(1, n + 1)
    ]


class TestRunWiring(unittest.TestCase):
    def _factory(self, review_results):
        """review_results 是每次复核的 (sufficient, missing)，按顺序取。"""
        seq = list(review_results)

        def factory():
            def review_for(goal, specs):
                return seq.pop(0) if seq else (True, [])
            return ScriptedBackend({}, decompose_for=lambda g, f: _drafts(),
                                   review_for=review_for)
        return factory

    def test_clean_run_is_recorded_as_first_round_pass(self):
        rec = plan_once(
            GOALS[0], arm="full", backend_factory=self._factory([(True, [])]),
            reviewer_factory=None, run_index=1,
            workspace_root=Path(tempfile.mkdtemp()),
        )
        self.assertEqual(rec.status, "ACCEPTED")
        self.assertTrue(rec.first_round_clean)
        self.assertFalse(rec.recovered)
        self.assertEqual(rec.attempts, 1)
        self.assertEqual(rec.max_parallel, 2)

    def test_rejected_then_accepted_counts_as_recovered(self):
        rec = plan_once(
            GOALS[0], arm="naive",
            backend_factory=self._factory([(False, ["缺了错误处理"]), (True, [])]),
            reviewer_factory=None, run_index=1,
            workspace_root=Path(tempfile.mkdtemp()),
        )
        self.assertEqual(rec.attempts, 2)
        self.assertFalse(rec.first_round_clean)
        self.assertTrue(rec.recovered)
        self.assertEqual(rec.findings_per_round[0], ["缺了错误处理"])

    def test_escalation_without_a_gate_lands_on_awaiting_human(self):
        """跑批不挂网关：升级要落在 AWAITING_HUMAN 并留下原因，否则这次实测白做。"""
        rec = plan_once(
            GOALS[0], arm="naive",
            backend_factory=self._factory([(False, ["同一个缺口"])] * 4),
            reviewer_factory=None, run_index=1,
            workspace_root=Path(tempfile.mkdtemp()),
        )
        self.assertEqual(rec.status, "AWAITING_HUMAN")
        self.assertEqual(rec.escalation_kind, "repeat")
        self.assertFalse(rec.recovered)

    def test_batch_writes_one_line_per_job(self):
        out = Path(tempfile.mkdtemp()) / "recs.jsonl"
        recs = run_batch(
            list(GOALS[:2]),
            arms={"full": self._factory([(True, [])] * 10),
                  "naive": self._factory([(True, [])] * 10)},
            reviewer_factory=None, repeat=1, out_path=out,
            workspace_root=Path(tempfile.mkdtemp()), workers=2,
        )
        self.assertEqual(len(recs), 4)
        self.assertEqual(len(out.read_text(encoding="utf-8").strip().splitlines()), 4)


def _rec(**kw) -> dict:
    base = dict(goal_id="wc", arm="full", run_index=1, status="ACCEPTED", attempts=1,
                tokens=1000, wall_seconds=1.0, subtasks=3, max_parallel=2,
                escalation_reason="", escalation_kind="none", first_round_clean=True,
                recovered=False, findings_per_round=[[]], structural_per_round=[[]], error="")
    base.update(kw)
    return PlanRecord(**base).to_dict()


class TestMetrics(unittest.TestCase):
    def test_recovery_rate_is_over_rejected_runs_only(self):
        """一轮就过的运行对「重生成有没有用」没有贡献，不能进分母。"""
        recs = [
            _rec(),
            _rec(first_round_clean=False, attempts=2, recovered=True),
            _rec(first_round_clean=False, attempts=3, recovered=False,
                 status="AWAITING_HUMAN", escalation_kind="cap"),
        ]
        m = arm_summary(recs)["full"]
        self.assertEqual(m["rejected_runs"], 2)
        self.assertEqual(m["recovered"], 1)
        self.assertEqual(m["recovery_rate"], 0.5)
        self.assertAlmostEqual(m["first_round_pass"], 1 / 3, places=2)

    def test_arms_are_separated(self):
        recs = [_rec(arm="full"), _rec(arm="naive", first_round_clean=False, attempts=2)]
        m = arm_summary(recs)
        self.assertEqual(m["full"]["first_round_pass"], 1.0)
        self.assertEqual(m["naive"]["first_round_pass"], 0.0)

    def test_errors_are_excluded(self):
        recs = [_rec(), _rec(error="boom")]
        m = arm_summary(recs)["full"]
        self.assertEqual(m["runs"], 1)
        self.assertEqual(m["errors"], 1)

    def test_loop_evidence_counts_real_regenerations(self):
        recs = [
            _rec(),
            _rec(first_round_clean=False, attempts=2,
                 findings_per_round=[["a"], ["b"]]),
            _rec(first_round_clean=False, attempts=2, status="AWAITING_HUMAN",
                 escalation_kind="repeat", findings_per_round=[["a"], ["a"]]),
        ]
        loop = loop_evidence(recs)
        self.assertEqual(loop["runs_with_regeneration"], 2)
        self.assertEqual(loop["max_attempts_seen"], 2)
        self.assertEqual(loop["escalated_runs"], 1)
        self.assertEqual(loop["second_round_changed"], 1)

    def test_escalation_kinds_are_classified_from_the_real_wording(self):
        """判错类别，风险 #17 的结论就反了 —— 这些字符串来自 escalation.py。"""
        self.assertEqual(plan_ab._escalation_kind(
            "连续 2 轮复核结论完全相同（阈值 2），重生成没有改变现实"), "repeat")
        self.assertEqual(plan_ab._escalation_kind(
            "已重生成 2 次仍未通过复核（阈值 max_regenerate=2）"), "cap")
        self.assertEqual(plan_ab._escalation_kind(
            "生成者无法产出合规拆解：供应商 500"), "model_failure")
        self.assertEqual(plan_ab._escalation_kind(""), "none")

    def test_summary_renders(self):
        text = plan_ab.render(summarize([_rec(), _rec(arm="naive", first_round_clean=False,
                                                     attempts=2, recovered=True)]))
        self.assertIn("重生成路径的证据", text)
        self.assertIn("naive", text)


if __name__ == "__main__":
    unittest.main()
