"""跨模型复核对照的用例表与指标（§12 M7 7.2）。

**这批测试验证不了「跨模型复核有没有用」** —— 那个只有真实模型能回答，
结论在 §11.11。这里钉的是三件会让那个结论失真的事：

  用例表本身立不立得住   负例真的完整、正例的缺陷真的落在语义层
  跑批把 arm 接对了没有  同模型 arm 必须走 reviewer_backend=None 那条默认分支
  指标算得对不对        TPR / FPR / J 的分母、以及出错记录怎么扣

第一条最容易被忽略：如果一个「完整」的负例其实结构就有问题，那测出来的假阳性
是我们自己造的，不是复核者的问题（同 §11.5a 任务集自检的道理）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cowork.bench import review_ab
from cowork.bench.review_ab import (
    ALL_CASES,
    ReviewRecord,
    arm_metrics,
    disagreements,
    free_half_credit,
    review_once,
    run_batch,
    select_cases,
    summarize,
)
from cowork.llm.scripted import ScriptedBackend
from cowork.plan import deterministic_review

BLOCKING = ("empty", "invalid_graph", "no_scope", "no_acceptance")


class TestCaseTable(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = [c.id for c in ALL_CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_family_has_exactly_one_complete_case(self):
        """两侧都要有数据，且负例不能只有一个家族 —— 否则 FPR 是单点估计。"""
        families = {c.family for c in ALL_CASES}
        self.assertGreaterEqual(len(families), 3)
        for fam in families:
            complete = [c for c in ALL_CASES if c.family == fam and c.complete]
            self.assertEqual(len(complete), 1, f"{fam} 的完整用例数不对")

    def test_defect_shapes_cover_more_than_missing_subtask(self):
        """§11.10 的局限之一是只测了「整个子任务缺失」。三种形态都要有。"""
        shapes = {c.defect for c in ALL_CASES if not c.complete}
        self.assertEqual(shapes, {"missing_subtask", "loose_criterion", "uncovered_seam"})

    def test_complete_cases_are_structurally_clean(self):
        """负例上结构检查一声都不该出 —— 出了就说明这个「完整」是我们自己写错的。"""
        for case in ALL_CASES:
            if not case.complete:
                continue
            issues = deterministic_review(case.root_goal, list(case.specs))
            self.assertEqual(
                [i.kind for i in issues], [], f"{case.id} 的负例结构不干净"
            )

    def test_defective_cases_still_reach_the_semantic_review(self):
        """正例不能被结构检查挡在语义复核之前，否则这一格根本没测到复核者。"""
        for case in ALL_CASES:
            if case.complete:
                continue
            kinds = {i.kind for i in deterministic_review(case.root_goal, list(case.specs))}
            self.assertFalse(
                kinds & set(BLOCKING), f"{case.id} 会被结构检查短路：{kinds}"
            )

    def test_semantic_only_defects_are_invisible_to_the_free_half(self):
        """验收标准太松、衔接没人验 —— 这两种结构检查看不见，正是语义复核的存在理由。

        它们要是也被 `fan_out` 之类顺带抓到，跨模型复核的成绩就被免费那一半垫高了。
        """
        for case in ALL_CASES:
            if case.defect not in ("loose_criterion", "uncovered_seam"):
                continue
            issues = deterministic_review(case.root_goal, list(case.specs))
            self.assertEqual([i.kind for i in issues], [], f"{case.id} 被结构检查抓到了")

    def test_every_case_explains_itself(self):
        """note 是判断一次假阳性到底是谁的问题的唯一依据，不能空着。"""
        for case in ALL_CASES:
            self.assertGreater(len(case.note), 10, case.id)

    def test_select_by_id_family_or_defect(self):
        self.assertEqual([c.id for c in select_cases("a_complete")], ["a_complete"])
        self.assertEqual({c.family for c in select_cases("docs")}, {"docs"})
        self.assertEqual(
            {c.defect for c in select_cases("loose_criterion")}, {"loose_criterion"}
        )
        self.assertEqual({c.id for c in select_cases("complete")},
                         {c.id for c in ALL_CASES if c.complete})
        self.assertEqual(len(select_cases(None)), len(ALL_CASES))


class TestRunWiring(unittest.TestCase):
    """跑批把 arm 接对了没有。用脚本后端 —— 这里测的是接线，不是判别力。"""

    def _factory(self, sufficient: bool, missing=()):
        return lambda: ScriptedBackend({}, review_for=lambda g, s: (sufficient, list(missing)))

    def test_same_model_arm_uses_the_default_branch(self):
        """对照组和实验组必须共用同一段代码，差别只有复核者是谁。"""
        case = next(c for c in ALL_CASES if c.complete)
        rec = review_once(
            case, arm="base", base_factory=self._factory(True),
            reviewer_factory=None, run_index=1,
        )
        self.assertFalse(rec.independent)
        self.assertTrue(rec.sufficient)
        self.assertEqual(rec.error, "")

    def test_cross_model_arm_asks_the_reviewer(self):
        case = next(c for c in ALL_CASES if c.complete)
        rec = review_once(
            case, arm="other",
            base_factory=self._factory(True),
            reviewer_factory=self._factory(False, ["复核者说缺了东西"]),
            run_index=1,
        )
        self.assertTrue(rec.independent)
        self.assertFalse(rec.sufficient)
        self.assertEqual(rec.missing, ["复核者说缺了东西"])

    def test_backend_failure_becomes_a_record_not_a_crash(self):
        """一次调用挂掉不能带塌整批 —— 跑批中途死掉等于钱白花。"""
        class Boom(ScriptedBackend):
            def review_decomposition(self, root_goal, specs):
                raise RuntimeError("供应商 500")

        case = next(c for c in ALL_CASES if c.complete)
        rec = review_once(
            case, arm="boom", base_factory=lambda: Boom({}),
            reviewer_factory=None, run_index=1,
        )
        self.assertIn("供应商 500", rec.error)
        self.assertIsNone(rec.sufficient)

    def test_run_batch_writes_one_line_per_job(self):
        cases = select_cases("report")
        out = Path(tempfile.mkdtemp()) / "recs.jsonl"
        recs = run_batch(
            cases,
            base_factory=self._factory(True),
            arms={"base": None, "other": self._factory(False, ["x"])},
            repeat=2,
            out_path=out,
            workers=2,
        )
        self.assertEqual(len(recs), len(cases) * 2 * 2)
        lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), len(recs))
        self.assertEqual({r["arm"] for r in lines}, {"base", "other"})
        self.assertIn("structural_caught", lines[0])


def _rec(**kw) -> dict:
    base = dict(
        case_id="x", family="f", defect="complete", complete=True, arm="a",
        independent=False, run_index=1, reviewer="scripted", sufficient=True,
        missing=[], structural=[], tokens=100, wall_seconds=0.1, error="",
    )
    base.update(kw)
    return ReviewRecord(**base).to_dict()


class TestMetrics(unittest.TestCase):
    def test_tpr_fpr_and_youden(self):
        recs = [
            # 4 个正例，报出 3 个
            _rec(complete=False, defect="missing_subtask", sufficient=False),
            _rec(complete=False, defect="missing_subtask", sufficient=False),
            _rec(complete=False, defect="loose_criterion", sufficient=False),
            _rec(complete=False, defect="loose_criterion", sufficient=True),
            # 4 个负例，误报 1 个
            _rec(complete=True, sufficient=True),
            _rec(complete=True, sufficient=True),
            _rec(complete=True, sufficient=True),
            _rec(complete=True, sufficient=False),
        ]
        m = arm_metrics(recs)["a"]
        self.assertEqual((m["tp"], m["fn"], m["fp"], m["tn"]), (3, 1, 1, 3))
        self.assertEqual(m["tpr"], 0.75)
        self.assertEqual(m["fpr"], 0.25)
        self.assertEqual(m["youden_j"], 0.5)
        self.assertEqual(m["recall_by_defect"]["missing_subtask"], 1.0)
        self.assertEqual(m["recall_by_defect"]["loose_criterion"], 0.5)

    def test_errors_are_excluded_not_counted_as_misses(self):
        """把调用失败算成「没报缺口」会把 FN 做多 —— 那是基础设施的账。"""
        recs = [
            _rec(complete=False, sufficient=False),
            _rec(complete=False, sufficient=None, error="boom"),
        ]
        m = arm_metrics(recs)["a"]
        self.assertEqual(m["positives"], 1)
        self.assertEqual(m["tpr"], 1.0)
        self.assertEqual(m["errors"], 1)

    def test_arms_are_scored_separately(self):
        recs = [
            _rec(arm="same", complete=False, sufficient=True),
            _rec(arm="cross", complete=False, sufficient=False, independent=True),
        ]
        m = arm_metrics(recs)
        self.assertEqual(m["same"]["tpr"], 0.0)
        self.assertEqual(m["cross"]["tpr"], 1.0)
        self.assertTrue(m["cross"]["independent"])

    def test_free_half_credit_is_tracked_separately(self):
        """结构检查抓到的那部分不该记在语义复核头上。"""
        recs = [
            _rec(complete=False, defect="missing_subtask", sufficient=False,
                 structural=[{"kind": "fan_out", "detail": "d", "tasks": []}]),
            _rec(complete=False, defect="loose_criterion", sufficient=False),
        ]
        free = free_half_credit(recs)
        self.assertEqual(free["positives_flagged_by_structure"]["missing_subtask"], "1/1")
        self.assertEqual(free["positives_flagged_by_structure"]["loose_criterion"], "0/1")
        self.assertEqual(free["structural_kinds"], {"fan_out": 1})

    def test_disagreements_surface_arm_splits(self):
        recs = [
            _rec(case_id="a_loose", arm="same", complete=False, sufficient=True),
            _rec(case_id="a_loose", arm="cross", complete=False, sufficient=False),
            _rec(case_id="a_complete", arm="same", sufficient=True),
            _rec(case_id="a_complete", arm="cross", sufficient=True),
        ]
        out = disagreements(recs)
        self.assertEqual([d["case_id"] for d in out], ["a_loose"])
        self.assertEqual(out[0]["flag_rate_by_arm"], {"same": 0.0, "cross": 1.0})

    def test_summary_renders(self):
        recs = [
            _rec(complete=False, sufficient=False),
            _rec(complete=True, sufficient=True),
        ]
        text = review_ab.render(summarize(recs))
        self.assertIn("TPR", text)
        self.assertIn("免费那一半", text)


if __name__ == "__main__":
    unittest.main()
