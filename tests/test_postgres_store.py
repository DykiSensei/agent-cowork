"""PostgresStore 回归测试。

连不上就 skip —— 需要先 `docker compose up -d postgres`。
重点验证 §10.5 那条不能将就的约束：checkpoints.context_json 里
produced 与 reasoning_trace 必须是两个顶层键，DB 层的 CHECK 会挡住扁平结构。
"""

import json
import unittest

from cowork.store.postgres import DEFAULT_DSN
from cowork.types import (
    Action,
    AgentContext,
    Artifact,
    Checkpoint,
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskState,
    TaskStatus,
)


def _pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(DEFAULT_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


@unittest.skipUnless(_pg_available(), f"Postgres 不可达 ({DEFAULT_DSN})")
class TestPostgresStore(unittest.TestCase):
    def setUp(self):
        from cowork.store.postgres import PostgresStore

        self.store = PostgresStore()
        self.spec = TaskSpec(
            goal="pg roundtrip",
            acceptance=[Criterion("c1", "落库能读回来")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace="."),
            scope=["a.py"],
        )
        self.store.save_task(TaskState(spec=self.spec, status=TaskStatus.RUNNING))

    def tearDown(self):
        with self.store.conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (self.spec.id,))
        self.store.close()

    def test_task_roundtrip(self):
        got = self.store.load_task(self.spec.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.spec.to_dict(), self.spec.to_dict())
        self.assertIs(got.status, TaskStatus.RUNNING)

    def test_checkpoint_roundtrip(self):
        ctx = AgentContext(
            task_spec=self.spec,
            produced=[Artifact(self.spec.id, "file", "a.py", "第一版")],
            reasoning_trace=[{"role": "assistant", "step": 1}],
        )
        cp = Checkpoint(task_id=self.spec.id, step=1, agent_context=ctx)
        self.store.save_checkpoint(cp)

        back = self.store.load_checkpoint(cp.id)
        self.assertEqual([a.content_ref for a in back.agent_context.produced], ["a.py"])
        self.assertEqual(len(back.agent_context.reasoning_trace), 1)

    def test_decision_roundtrip_carries_the_m6_fields(self):
        """两个存储必须写同一份东西 —— M6 §9 的两条缺口在 pg 上也要闭合。"""
        from cowork.types import Decider, DecisionRecord

        changes = {"added_criteria": [{"id": "c2", "description": "还要处理空行"}]}
        suggestion = {"action": "ABANDON", "rationale": "没救了",
                      "complexity_score": 0.8, "spec_changes": {}}
        self.store.save_decision(DecisionRecord(
            task_id=self.spec.id, trigger=[], decider=Decider.LLM,
            action=Action.MODIFY_TASK, rationale="补一条", spec_changes=changes,
            escalation_reason="顶层 MODIFY_TASK", suggestion=suggestion,
        ))

        back = self.store.decisions_for(self.spec.id)[0]
        self.assertEqual(back.spec_changes, changes)
        self.assertEqual(back.suggestion["action"], "ABANDON")

    def test_event_seq_is_assigned_by_the_db(self):
        from cowork.types import TaskEvent

        a = self.store.append_event(TaskEvent(task_id=self.spec.id, kind="log", text="一"))
        b = self.store.append_event(TaskEvent(task_id=self.spec.id, kind="log", text="二"))
        self.assertEqual((a.seq, b.seq), (1, 2))
        self.assertEqual([e.text for e in self.store.events_for(self.spec.id)], ["一", "二"])
        self.assertEqual([e.seq for e in self.store.events_for(self.spec.id, 1)], [2])

    def test_events_go_away_with_their_task(self):
        """外键 ON DELETE CASCADE：任务删了，时间线不该留成孤儿。"""
        from cowork.types import TaskEvent

        self.store.append_event(TaskEvent(task_id=self.spec.id, kind="log", text="x"))
        with self.store.conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (self.spec.id,))
        self.assertEqual(self.store.events_for(self.spec.id), [])

    def test_db_rejects_flat_context(self):
        """存成一坨扁平消息列表的话，§6 整节都无法实现——所以 DB 层直接拒。"""
        import psycopg

        flat = json.dumps({"messages": [{"role": "assistant"}]})
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.store.conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO checkpoints (id, task_id, step, context_json, created_at)
                       VALUES (%s,%s,%s,%s,%s)""",
                    ("ckpt_flat_bad", self.spec.id, 1, flat, 0.0),
                )


if __name__ == "__main__":
    unittest.main()
