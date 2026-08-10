"""写入侧复核对照的用例表与指标（§12 M8）。

**这批测试回答不了「写入侧复核有没有用」** —— 那要真实模型跑 `bench-decide`。
这里钉的是三件会让那个结论失真的事：

  用例表立不立得住   负例真的没问题、正例的缺陷真的在 `_apply_changes` 的可达范围内
  跑批测的是什么     必须直接调复核者，不能混进重做循环
  指标算得对不对     TPR / FPR / J 的分母，以及出错记录怎么扣

第三条尤其要盯：出错的记录**不能算成「没报问题」**。复核者调不动模型和
复核者认为没问题，在账面上都是「没 flag」，但结论相反（同 §11.14 里
「这家不报缓存」和「没命中」要分开记的道理）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cowork.bench.decide_ab import (
    ALL_CASES,
    WriteRecord,
    arm_metrics,
    by_defect,
    review_once,
    run_batch,
    select_cases,
    summarize,
    unstable_cases,
)
from cowork.llm.scripted import ScriptedBackend

# `Architect._apply_changes` 真正认的字段。用例表里的改动只能落在这些上面 ——
# 写一个系统根本执行不了的改动，测的是幻想。
APPLICABLE = {"goal", "added_criteria", "scope", "token_budget", "max_steps",
              "deadline_s", "model"}


class TestCaseTable(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = [c.id for c in ALL_CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_both_sides_have_enough_cases(self):
        """只测一侧一定会得出错误结论（M5a 第一版的教训）。"""
        sound = [c for c in ALL_CASES if c.sound]
        unsound = [c for c in ALL_CASES if not c.sound]
        self.assertGreaterEqual(len(sound), 3, "负例太少，FPR 会是单点估计")
        self.assertGreaterEqual(len(unsound), 5, "正例太少，TPR 不可信")

    def test_negatives_are_spread_across_families(self):
        """负例集中在一个家族的话，FPR 测的是那个家族而不是这类判断。"""
        families = {c.family for c in ALL_CASES if c.sound}
        self.assertGreaterEqual(len(families), 3)

    def test_defect_shapes_are_diverse(self):
        """一种形态测不出盲区在哪。至少覆盖四种可达的放松手法。"""
        shapes = {c.defect for c in ALL_CASES if not c.sound}
        for must in ("goal_loosened", "vague_criterion", "limit_raised", "scope_widened"):
            self.assertIn(must, shapes)

    def test_every_change_is_actually_applicable(self):
        """改动必须落在 `_apply_changes` 认的字段上。

        比如「删掉一条验收标准」在这个系统里根本发生不了（acceptance 只能追加），
        写那种用例等于在测一个不存在的威胁。
        """
        for case in ALL_CASES:
            keys = set(case.verdict.spec_changes)
            self.assertTrue(keys, f"{case.id} 没有任何改动")
            self.assertTrue(
                keys <= APPLICABLE,
                f"{case.id} 改了 _apply_changes 不认的字段: {keys - APPLICABLE}",
            )

    def test_cases_would_not_be_escalated_before_review(self):
        """用例必须真的能走到复核那一步。

        顶层任务（parent_id=None）的 MODIFY_TASK 会被确定性规则直接升级，
        complexity_score 超阈值也一样 —— 那样的用例永远轮不到复核者，
        测出来的是另一条路径。
        """
        from cowork.policy import DEFAULT_POLICY

        for case in ALL_CASES:
            self.assertIsNotNone(case.spec.parent_id, f"{case.id} 是顶层任务")
            self.assertLess(
                case.verdict.complexity_score, DEFAULT_POLICY.complexity_threshold,
                f"{case.id} 的自评分会先触发升级",
            )

    def test_every_case_explains_itself(self):
        """负例写「为什么这个改动恰当」，正例写「问题在哪」——
        跨模型复核的争议全在这两句话上，没有它就没法判断一次误报是谁的问题。
        """
        for case in ALL_CASES:
            self.assertGreaterEqual(len(case.note), 20, f"{case.id} 的 note 太短")
            if case.sound:
                self.assertEqual(case.defect, "", f"{case.id} 是负例不该有 defect")
            else:
                self.assertTrue(case.defect, f"{case.id} 是正例但没写缺陷形态")

    def test_sound_cases_never_touch_goal_or_scope_or_limits(self):
        """负例的判据要能一句话说清：只追加验收标准，不动目标、边界和上限。

        这条不是风格要求 —— 它是「改得对」的操作性定义。一个动了 goal 的改动
        是不是恰当，需要读懂上下文才能判断，那种用例放进负例就是在给自己挖坑
        （§11.11 第一轮栽的正是这个）。
        """
        for case in ALL_CASES:
            if not case.sound:
                continue
            keys = set(case.verdict.spec_changes)
            self.assertEqual(
                keys, {"added_criteria"},
                f"{case.id} 是负例，却改了 {keys - {'added_criteria'}}",
            )

    def test_select_cases_filters(self):
        self.assertEqual(len(select_cases()), len(ALL_CASES))
        self.assertTrue(all(c.sound for c in select_cases("sound")))
        self.assertTrue(all(not c.sound for c in select_cases("unsound")))
        self.assertTrue(
            all(c.defect == "goal_loosened" for c in select_cases("goal_loosened"))
        )
        with self.assertRaises(SystemExit):
            select_cases("没有这个东西")


class TestRunner(unittest.TestCase):
    def test_review_once_calls_the_reviewer_directly(self):
        """要测的是判别力，不是重做循环。混进循环的话一次误报会带出一次重做，
        记录就不再是「对这个改动怎么判」的干净样本。
        """
        seen = {}

        def spec_review(spec, signals, verdict):
            seen["spec_id"] = spec.id
            seen["signal"] = signals[0].type.value
            seen["changes"] = dict(verdict.spec_changes)
            return False, ["有问题"]

        case = next(c for c in ALL_CASES if not c.sound)
        backend = ScriptedBackend({}, spec_review_for=spec_review)
        rec = review_once(case, arm="t", reviewer_factory=lambda: backend, run_index=1)

        self.assertTrue(rec.flagged)
        self.assertEqual(seen["spec_id"], case.spec.id)
        self.assertEqual(seen["changes"], case.verdict.spec_changes)
        # 复核者只被问了一次：没有重做
        self.assertEqual(backend.spec_review_calls, 1)

    def test_failures_are_recorded_not_raised(self):
        def boom(*_):
            raise RuntimeError("端点 500")

        case = ALL_CASES[0]
        backend = ScriptedBackend({}, spec_review_for=boom)
        rec = review_once(case, arm="t", reviewer_factory=lambda: backend, run_index=1)

        self.assertTrue(rec.error)
        self.assertIsNone(rec.ok)
        self.assertFalse(rec.flagged)

    def test_run_batch_writes_one_line_per_job(self):
        cases = list(ALL_CASES[:3])
        arms = {
            "always": lambda: ScriptedBackend({}, spec_review_for=lambda *_: (False, ["x"])),
            "never": lambda: ScriptedBackend({}, spec_review_for=lambda *_: (True, [])),
        }
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "decide_ab.jsonl"
            recs = run_batch(cases, arms=arms, repeat=2, out_path=out, workers=2)
            lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(recs), 3 * 2 * 2)
        self.assertEqual(len(lines), len(recs))
        self.assertEqual({x["arm"] for x in lines}, {"always", "never"})


def _rec(**kw) -> dict:
    base = {
        "case_id": "c", "family": "f", "defect": "sound", "sound": True,
        "arm": "a", "run_index": 1, "reviewer": "r", "ok": True,
        "findings": [], "tokens": 10, "wall_seconds": 0.1, "error": "",
    }
    base.update(kw)
    base["flagged"] = base["ok"] is False
    return base


class TestMetrics(unittest.TestCase):
    def test_perfect_reviewer_scores_one(self):
        recs = [
            _rec(sound=False, defect="goal_loosened", ok=False),
            _rec(sound=True, defect="sound", ok=True),
        ]
        m = arm_metrics(recs)["a"]
        self.assertEqual(m["TPR"], 1.0)
        self.assertEqual(m["FPR"], 0.0)
        self.assertEqual(m["J"], 1.0)

    def test_reviewer_that_flags_everything_scores_zero(self):
        """逢改动必报的复核者 TPR 也是满分 —— 所以只看 TPR 一定会选错模型。"""
        recs = [
            _rec(sound=False, defect="goal_loosened", ok=False),
            _rec(sound=True, defect="sound", ok=False),
        ]
        m = arm_metrics(recs)["a"]
        self.assertEqual(m["TPR"], 1.0)
        self.assertEqual(m["FPR"], 1.0)
        self.assertEqual(m["J"], 0.0)

    def test_errors_are_excluded_not_counted_as_silence(self):
        """「调不动模型」和「认为没问题」在账面上都是没 flag，但结论相反。"""
        recs = [
            _rec(sound=False, ok=False),
            _rec(sound=False, ok=None, error="boom"),
        ]
        m = arm_metrics(recs)["a"]
        self.assertEqual(m["errors"], 1)
        self.assertEqual(m["n_unsound"], 1, "出错的那条不该进分母")
        self.assertEqual(m["TPR"], 1.0)

    def test_by_defect_shows_the_blind_spots(self):
        recs = [
            _rec(sound=False, defect="goal_loosened", ok=False),
            _rec(sound=False, defect="vague_criterion", ok=True),
        ]
        d = by_defect(recs)["a"]
        self.assertEqual(d["goal_loosened"]["rate"], 1.0)
        self.assertEqual(d["vague_criterion"]["rate"], 0.0)

    def test_unstable_cases_are_surfaced(self):
        """同一份输入上翻面的模型是弱证据（§11.11）。"""
        recs = [
            _rec(case_id="x", ok=False, run_index=1),
            _rec(case_id="x", ok=True, run_index=2),
            _rec(case_id="y", ok=True, run_index=1),
            _rec(case_id="y", ok=True, run_index=2),
        ]
        unstable = unstable_cases(recs)
        self.assertEqual([u["case_id"] for u in unstable], ["x"])

    def test_summary_renders(self):
        from cowork.bench.decide_ab import render

        recs = [_rec(sound=False, defect="goal_loosened", ok=False, findings=["目标被改松"])]
        text = render(summarize(recs))
        self.assertIn("判别力", text)
        self.assertIn("goal_loosened", text)


if __name__ == "__main__":
    unittest.main()
