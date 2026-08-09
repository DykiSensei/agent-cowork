"""PostgresStore 回归测试。

连不上就 skip —— 需要先 `docker compose up -d postgres`。
重点验证 §10.5 那条不能将就的约束：checkpoints.context_json 里
produced 与 reasoning_trace 必须是两个顶层键，DB 层的 CHECK 会挡住扁平结构。
"""

import json
import unittest

from cowork.store.postgres import DEFAULT_DSN
from cowork.types import (
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
