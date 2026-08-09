"""M2 实测工具本身的测试（§12 M2）。

实测工具出错的后果比普通 bug 严重：它会安静地产出**看起来合理的错数字**，
然后这些数字被写进 policy.py 当成结论。所以这里钉三件事：
任务集的不变量、仪表化字段确实被填上、分析器的算法在已知输入上给出已知答案。

全部离线跑（脚本后端），不需要 key。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.bench import ALL_TASKS, BENCH_TASKS, PROBE_TASKS, Category
from cowork.bench.analyze import (
    complexity_roc,
    interrupts_vs_success,
    render,
    step_deadline,
    summarize,
    task_set_health,
)
from cowork.bench.runner import _escalation_kind, run_once
from cowork.bench.tasks import BY_ID
from cowork.escalation import should_escalate
from cowork.llm import ArchitectVerdict
from cowork.llm.scripted import ScriptedBackend
from cowork.policy import Policy
from cowork.types import TaskSpec, TaskState


class TestTaskSet(unittest.TestCase):
    def test_covers_four_shapes(self):
        m2_cats = [c for c in Category if c is not Category.PROBE_AB]
        for c in m2_cats:
            ts = [t for t in BENCH_TASKS if t.category is c]
            self.assertGreaterEqual(len(ts), 3, f"{c} 只有 {len(ts)} 个任务")
        self.assertGreaterEqual(len(BENCH_TASKS), 10)  # §12 M2 前置：10–20 个

    def test_m2_task_set_excludes_m3_arms(self):
        """M2 的结论要能被原样复现，默认任务集不能因为后续里程碑变化。"""
        self.assertTrue(all(t.category is not Category.PROBE_AB for t in BENCH_TASKS))
        self.assertEqual(len(ALL_TASKS), len(BENCH_TASKS) + len(PROBE_TASKS))

    def test_probe_arms_differ_only_in_silence_policy(self):
        """三个 arm 的 goal / 验收 / scope 必须完全一致，否则差值归因不了。"""
        ws = Path(tempfile.mkdtemp())
        specs = [t.spec(ws) for t in PROBE_TASKS]
        self.assertEqual(len({s.goal for s in specs}), 1)
        self.assertEqual(len({tuple(s.scope) for s in specs}), 1)
        self.assertEqual(
            len({tuple((c.id, c.description) for c in s.acceptance) for s in specs}), 1
        )
        self.assertEqual(
            {s.silence_policy.value for s in specs}, {"TRUST", "PROBE"}
        )

    def test_ids_unique_and_documented(self):
        ids = [t.id for t in ALL_TASKS]
        self.assertEqual(len(ids), len(set(ids)))
        for t in ALL_TASKS:
            self.assertTrue(t.hidden.strip(), f"{t.id} 没写隐藏项说明")
            self.assertTrue(t.goal.strip())

    def test_escalation_labels_match_category(self):
        for t in BENCH_TASKS:
            self.assertEqual(
                t.should_escalate,
                t.category is Category.ESCALATE,
                f"{t.id} 的人工标注与类别不一致",
            )

    def test_all_tasks_are_subtasks(self):
        """顶层任务 + MODIFY_TASK 会命中确定性升级，complexity_score 根本用不上。"""
        ws = Path(tempfile.mkdtemp())
        for t in BENCH_TASKS:
            self.assertIsNotNone(t.spec(ws).parent_id, f"{t.id} 是顶层任务")

    def test_verify_script_hides_the_answer(self):
        """用例表必须是压缩的：read_file 读到 verify.py 也拿不到期望值。"""
        ws = Path(tempfile.mkdtemp())
        t = BY_ID["r2_chunk_drop_tail"]
        t.materialize(ws)
        src = (ws / "verify.py").read_text(encoding="utf-8")
        self.assertNotIn("[[1, 2], [3, 4]]", src)
        self.assertIn("zlib.decompress", src)

    def test_intent_check_never_lands_in_workspace(self):
        """意图检查脚本进了 workspace 就等于把答案发给 Subagent。"""
        ws = Path(tempfile.mkdtemp())
        for t in BENCH_TASKS:
            t.materialize(ws)
        self.assertFalse((ws / "intent_check.py").exists())


class TestEscalationCoupling(unittest.TestCase):
    """runner._escalation_kind 靠理由文案区分「确定性升级」和「自评超阈值」。

    这条断言是那处字符串耦合的保险丝：改了 escalation.py 的措辞，这里先红。
    """

    def _state(self, spec):
        return TaskState(spec=spec)

    def test_complexity_reason_prefix(self):
        t = BY_ID["r1_palindrome_empty"]
        spec: TaskSpec = t.spec(Path(tempfile.mkdtemp()))
        verdict = ArchitectVerdict(action="CONTINUE", rationale="x", complexity_score=0.99)
        reason = should_escalate(Policy(), spec, self._state(spec), [], verdict)
        self.assertIsNotNone(reason)
        self.assertEqual(_escalation_kind(reason), "complexity")

    def test_deterministic_reason_is_not_complexity(self):
        t = BY_ID["e4_irreversible"]  # 验收命令含 curl
        spec = t.spec(Path(tempfile.mkdtemp()))
        verdict = ArchitectVerdict(action="CONTINUE", rationale="x", complexity_score=0.0)
        reason = should_escalate(Policy(), spec, self._state(spec), [], verdict)
        self.assertIsNotNone(reason)
        self.assertEqual(_escalation_kind(reason), "deterministic")

    def test_no_escalation(self):
        self.assertEqual(_escalation_kind(None), "none")


# --------------------------------------------------------------------------- #

_FINISH = Finish(output={"file": "solution.py", "function": "f"}, summary="done")

_WORD_COUNT = "def count_words(s):\n    return len(s.split())\n"
_PALI_NAIVE = (
    "def is_palindrome(s):\n"
    "    n = [c.lower() for c in s if c.isalnum()]\n"
    "    return n == n[::-1]\n"
)
_PALI_OK = (
    "def is_palindrome(s):\n"
    "    n = [c.lower() for c in s if c.isalnum()]\n"
    "    return bool(n) and n == n[::-1]\n"
)


def _script(sources: dict[int, str]) -> ScriptedBackend:
    steps = {}
    for rev, src in sources.items():
        steps[(rev, 0)] = ToolCall("write_file", {"path": "solution.py", "content": src})
        steps[(rev, 1)] = _FINISH
    return ScriptedBackend(
        steps,
        verdict_for=lambda spec, sigs: ArchitectVerdict(
            action="MODIFY_TASK",
            rationale="补一条验收标准",
            complexity_score=0.3,
            spec_changes={"added_criteria": [{"id": "cx", "description": "补充约定"}]},
        ),
    )


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bench-test-"))

    def test_pass_task_records_instrumentation(self):
        rec = run_once(
            BY_ID["p1_word_count"],
            backend_factory=lambda: _script({1: _WORD_COUNT}),
            run_index=1,
            root=self.root,
        )
        self.assertEqual(rec.error, "")
        self.assertEqual(rec.status, "COMPLETED")
        self.assertEqual(rec.interrupts, 0)
        self.assertTrue(rec.step_seconds, "没记到 step 耗时")
        self.assertTrue(rec.checkpoint_seconds, "没记到 checkpoint 耗时")
        self.assertTrue(rec.task_trace, "没记到状态轨迹")
        self.assertIs(rec.intent_ok, True)

    def test_one_rebase_task_interrupts_then_completes(self):
        rec = run_once(
            BY_ID["r1_palindrome_empty"],
            backend_factory=lambda: _script({1: _PALI_NAIVE, 2: _PALI_OK}),
            run_index=1,
            root=self.root,
        )
        self.assertEqual(rec.error, "")
        self.assertEqual(rec.interrupts, 1)
        self.assertEqual(rec.status, "COMPLETED")
        self.assertEqual(rec.rebase_count, 1)
        self.assertEqual([s["type"] for s in rec.signals], ["TEST_FAILED"])
        self.assertEqual(rec.decisions[0]["score"], 0.3)
        self.assertIs(rec.intent_ok, True)

    def test_record_is_json_serializable(self):
        rec = run_once(
            BY_ID["p1_word_count"],
            backend_factory=lambda: _script({1: _WORD_COUNT}),
            run_index=1,
            root=self.root,
        )
        json.dumps(rec.to_dict(), ensure_ascii=False)

    def test_failure_is_captured_not_raised(self):
        """一次运行炸了不能把整批带走。"""

        class Boom:
            name = "boom"

            def next_step(self, ctx):
                raise RuntimeError("炸了")

        rec = run_once(
            BY_ID["p1_word_count"],
            backend_factory=Boom,
            run_index=1,
            root=self.root,
        )
        self.assertIn("炸了", rec.error)
        self.assertEqual(rec.status, "ERROR")


# --------------------------------------------------------------------------- #


def _rec(**kw) -> dict:
    base = {
        "task_id": "t", "category": "PASS", "run_index": 1, "backend": "fake",
        "status": "COMPLETED", "revision": 1, "steps": 2, "interrupts": 0,
        "tokens": 1000, "wall_seconds": 1.0, "rebase_count": 0, "completed": True,
        "intent_ok": True, "intent_detail": "", "step_seconds": [1.0, 2.0],
        "checkpoint_seconds": [0.001], "calls": [], "decisions": [], "signals": [],
        "task_trace": [{"at": 0.0, "status": "RUNNING", "step": 0,
                        "interrupts": 0, "tokens": 0}],
        "token_budget": 10_000, "should_escalate": False, "error": "",
    }
    base.update(kw)
    return base


class TestAnalyze(unittest.TestCase):
    def test_conditional_success_uses_reached_k_as_denominator(self):
        recs = [
            _rec(interrupts=0, completed=True),
            _rec(interrupts=1, completed=True),
            _rec(interrupts=2, completed=False, status="FAILED"),
        ]
        rows = {r["reached_k_interrupts"]: r for r in
                interrupts_vs_success(recs)["conditional_success"]}
        self.assertEqual(rows[0]["runs"], 3)
        self.assertEqual(rows[1]["runs"], 2)   # 被中断过 >=1 次的
        self.assertEqual(rows[2]["rate"], 0.0)  # 到 2 次的那条最终没成

    def test_roc_separates_perfectly_when_scores_do(self):
        recs = [
            _rec(should_escalate=True,
                 decisions=[{"score": 0.9, "escalation_kind": "none"}]),
            _rec(should_escalate=False,
                 decisions=[{"score": 0.1, "escalation_kind": "none"}]),
        ]
        roc = complexity_roc(recs)
        self.assertEqual(roc["auc"], 1.0)
        self.assertEqual(roc["n_pos"], 1)
        self.assertEqual(roc["n_neg"], 1)

    def test_checkpoint_overhead_ratio(self):
        recs = [_rec(step_seconds=[1.0, 1.0], checkpoint_seconds=[0.01, 0.01])]
        self.assertAlmostEqual(step_deadline(recs)["checkpoint_overhead_ratio"], 0.01)

    def test_degenerate_task_is_flagged(self):
        """ONE_REBASE 类零中断 = 模型自己避开了，这批数据不能用（§11.5a）。"""
        recs = [_rec(task_id="r_x", category="ONE_REBASE", interrupts=0) for _ in range(3)]
        self.assertEqual(task_set_health(recs)["degenerate"], ["r_x"])

    def test_render_does_not_crash_on_minimal_input(self):
        self.assertIn("任务集自检", render(summarize([_rec()])))


if __name__ == "__main__":
    unittest.main()
