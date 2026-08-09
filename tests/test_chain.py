"""v0.1 要验证的那条链路，端到端。

L0 信号(TEST_FAILED) -> 中断 -> 架构师决策 -> REBASE -> 恢复 -> COMPLETED
"""

import json
import shutil
import unittest
from pathlib import Path

from cowork import demo
from cowork.types import Action, Decider, ResumeMode, TaskStatus


class TestChain(unittest.TestCase):
    def setUp(self):
        self.orch, self.ws = demo.build()
        self.orch.log = lambda _msg: None

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_full_chain(self):
        result = self.orch.run()
        state = result.state

        self.assertIs(state.status, TaskStatus.COMPLETED)
        self.assertEqual(state.spec.revision, 2, "REBASE 后 revision 应该 +1")
        self.assertEqual(state.interrupt_count, 1, "应恰好中断一次")
        self.assertEqual(
            result.output, {"file": "solution.py", "function": "is_palindrome"}
        )

        # 最终落盘的是修正后的实现，而不是第一版
        final = (Path(self.ws) / "solution.py").read_text(encoding="utf-8")
        self.assertIn("isalnum", final)

    def test_signal_is_hard_and_preempting(self):
        self.orch.run()
        sigs = self.orch.store.signals_for(self.orch.state.spec.id)
        hard = [s for s in sigs if s.type.value == "TEST_FAILED"]
        self.assertEqual(len(hard), 1)
        self.assertEqual(hard[0].level.value, "L0")
        self.assertEqual(hard[0].source.value, "RUNTIME", "硬信号必须由 Runtime 产生")
        self.assertEqual(hard[0].disposition.value, "PREEMPTED")
        self.assertIn("FAIL: is_palindrome", hard[0].raw_evidence or "")

    def test_decision_recorded_and_escalated(self):
        result = self.orch.run()
        self.assertEqual(len(result.decisions), 1)
        d = result.decisions[0]

        self.assertIs(d.action, Action.MODIFY_TASK)
        self.assertIs(d.resume_mode, ResumeMode.REBASE, "goal 未变、只改验收标准 -> REBASE")
        # 顶层任务 + MODIFY_TASK 命中 §7.2 确定性下限，LLM 无权覆盖
        self.assertIsNotNone(d.escalation_reason)
        self.assertIn("顶层任务", d.escalation_reason)
        self.assertIs(d.decider, Decider.HUMAN)

        # DecisionRecord 必须落库（§7.3 可见性）
        stored = self.orch.store.decisions_for(self.orch.state.spec.id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].id, d.id)

    def test_checkpoint_keeps_produced_and_trace_separate(self):
        """§10.5 唯一不能将就的地方。"""
        self.orch.run()
        rows = self.orch.store.conn.execute(
            "SELECT context_json FROM checkpoints ORDER BY rowid"
        ).fetchall()
        self.assertGreater(len(rows), 0)
        for (raw,) in rows:
            ctx = json.loads(raw)
            self.assertIn("produced", ctx)
            self.assertIn("reasoning_trace", ctx)
            self.assertIsInstance(ctx["produced"], list)
            self.assertIsInstance(ctx["reasoning_trace"], list)

    def test_rebase_cleared_the_trace(self):
        """恢复后的第一个 checkpoint 不应带着旧 revision 的推理痕迹。"""
        self.orch.run()
        rows = self.orch.store.conn.execute(
            "SELECT context_json FROM checkpoints ORDER BY rowid"
        ).fetchall()
        contexts = [json.loads(r[0]) for r in rows]

        rev2 = [c for c in contexts if c["task_spec"]["revision"] == 2]
        self.assertTrue(rev2, "应该有 revision=2 的 checkpoint")
        first_rev2 = rev2[0]

        # produced 跨恢复保留
        self.assertTrue(first_rev2["produced"], "produced 应跨 REBASE 保留")
        # 旧目标的推理痕迹不该跟过来：rev2 的第一个 checkpoint 里
        # trace 只包含本轮新产生的条目
        self.assertLessEqual(len(first_rev2["reasoning_trace"]), 3)
        # 摘要作为只读上下文被注入
        kinds = [a["kind"] for a in first_rev2["injected"]]
        self.assertIn("summary", kinds)


if __name__ == "__main__":
    unittest.main()
