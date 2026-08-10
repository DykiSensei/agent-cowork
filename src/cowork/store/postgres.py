"""Postgres 实现（§10.2 正式选型）。需要 `pip install "psycopg[binary]"`。

表结构见仓库根的 schema.sql，由 docker-compose 的 initdb 自动建。
DSN 默认：postgresql://cowork:cowork@localhost:5433/cowork
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..types import (
    Action,
    AgentContext,
    Artifact,
    Checkpoint,
    Decider,
    DecisionRecord,
    ResumeMode,
    Signal,
    TaskEvent,
    TaskSpec,
    TaskState,
    TaskStatus,
)

DEFAULT_DSN = os.environ.get(
    "COWORK_PG_DSN", "postgresql://cowork:cowork@localhost:5433/cowork"
)


# 建表由 docker-compose 的 initdb 跑 schema.sql，但那**只在第一次创建数据卷时执行**。
# 已经存在的库不会自动拿到后加的列和表 —— 加字段之后连老库，报的是
# 「column does not exist」，而那时候人已经在跑任务了。
# 所以连上就跑一遍这几条幂等 DDL，和 SqliteStore._migrate() 是同一个用意。
_MIGRATIONS = """
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS spec_changes_json JSONB;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS suggestion_json   JSONB;
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL DEFAULT '',
    ref_id       TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   DOUBLE PRECISION NOT NULL,
    UNIQUE (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ev_task ON events(task_id, seq);
-- events.task_id 曾经是 REFERENCES tasks(id)，那是个错的约束：**事件是线程级的，
-- 不是任务级的**。复合任务的 root 线程按设计就没有 tasks 行（`Scheduler` 拿到的
-- 是一组现成的 spec，没人建过那个父任务 —— 见 `views._synthetic_parent`），
-- 而分层结果、拆解复核、冲突仲裁全都写在 root 上。
-- 于是在 Postgres 上这些写入被外键拒绝，又被 `Scheduler._event()` 的 except 吞掉：
-- **复合线程的时间线在 PG 上整个是空的，一条报错都不会出现**。
-- SQLite 不强制外键，所以测试全绿。删掉这个约束，别再给 events 加外键。
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_task_id_fkey;
"""


class PostgresStore:
    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        import psycopg  # 延迟导入：没装 psycopg 也能用 SqliteStore
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute(_MIGRATIONS)

    # -- tasks ------------------------------------------------------------- #

    def save_task(self, state: TaskState) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO tasks (id, parent_id, revision, spec_json, status, agent_id,
                                      current_step, checkpoint_id, interrupt_count,
                                      tokens_used, started_at, artifacts_json, signals_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                     revision=EXCLUDED.revision, spec_json=EXCLUDED.spec_json,
                     status=EXCLUDED.status, agent_id=EXCLUDED.agent_id,
                     current_step=EXCLUDED.current_step,
                     checkpoint_id=EXCLUDED.checkpoint_id,
                     interrupt_count=EXCLUDED.interrupt_count,
                     tokens_used=EXCLUDED.tokens_used, started_at=EXCLUDED.started_at,
                     artifacts_json=EXCLUDED.artifacts_json,
                     signals_json=EXCLUDED.signals_json""",
                (
                    state.spec.id,
                    state.spec.parent_id,
                    state.spec.revision,
                    json.dumps(state.spec.to_dict()),
                    state.status.value,
                    state.agent_id,
                    state.current_step,
                    state.checkpoint_id,
                    state.interrupt_count,
                    state.tokens_used,
                    state.started_at,
                    json.dumps(state.artifacts),
                    json.dumps(state.signal_log),
                ),
            )

    def _row_to_task(self, r: dict[str, Any]) -> TaskState:
        return TaskState(
            spec=TaskSpec.from_dict(r["spec_json"]),
            status=TaskStatus(r["status"]),
            agent_id=r["agent_id"],
            current_step=r["current_step"],
            checkpoint_id=r["checkpoint_id"],
            interrupt_count=r["interrupt_count"],
            artifacts=r["artifacts_json"],
            signal_log=r["signals_json"],
            tokens_used=r["tokens_used"],
            started_at=r["started_at"],
        )

    def load_task(self, task_id: str) -> TaskState | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
            r = cur.fetchone()
        return self._row_to_task(r) if r else None

    def list_tasks(self) -> list[TaskState]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks")
            return [self._row_to_task(r) for r in cur.fetchall()]

    # -- checkpoints ------------------------------------------------------- #

    def save_checkpoint(self, cp: Checkpoint) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO checkpoints (id, task_id, step, context_json, created_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    cp.id,
                    cp.task_id,
                    cp.step,
                    json.dumps(cp.agent_context.to_dict()),
                    cp.created_at,
                ),
            )
        return cp.id

    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM checkpoints WHERE id=%s", (checkpoint_id,))
            r = cur.fetchone()
        if not r:
            return None
        ctx = AgentContext.from_dict(r["context_json"])
        return Checkpoint(
            id=r["id"],
            task_id=r["task_id"],
            step=r["step"],
            agent_context=ctx,
            artifacts=[a.id for a in ctx.produced],
            created_at=r["created_at"],
        )

    # -- signals ----------------------------------------------------------- #

    def save_signal(self, sig: Signal) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO signals (id, task_id, level, type, source, payload_json,
                                        raw_evidence, disposition, created_at, consumed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                     disposition=EXCLUDED.disposition, consumed_at=EXCLUDED.consumed_at""",
                (
                    sig.id,
                    sig.task_id,
                    sig.level.value,
                    sig.type.value,
                    sig.source.value,
                    json.dumps(sig.payload),
                    sig.raw_evidence,
                    sig.disposition.value,
                    sig.created_at,
                    sig.consumed_at,
                ),
            )

    def signals_for(self, task_id: str) -> list[Signal]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM signals WHERE task_id=%s ORDER BY created_at", (task_id,)
            )
            rows = cur.fetchall()
        return [
            Signal.from_dict(
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "level": r["level"],
                    "type": r["type"],
                    "source": r["source"],
                    "payload": r["payload_json"],
                    "raw_evidence": r["raw_evidence"],
                    "disposition": r["disposition"],
                    "created_at": r["created_at"],
                    "consumed_at": r["consumed_at"],
                }
            )
            for r in rows
        ]

    # -- decisions --------------------------------------------------------- #

    def save_decision(self, dec: DecisionRecord) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO decisions (id, task_id, trigger_signal_ids, decider,
                                          complexity_score, escalation_reason, action,
                                          new_spec_json, spec_changes_json, suggestion_json,
                                          resume_mode, rationale, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    dec.id,
                    dec.task_id,
                    json.dumps(dec.trigger),
                    dec.decider.value,
                    dec.complexity_score,
                    dec.escalation_reason,
                    dec.action.value,
                    json.dumps(dec.new_spec.to_dict()) if dec.new_spec else None,
                    json.dumps(dec.spec_changes) if dec.spec_changes else None,
                    json.dumps(dec.suggestion) if dec.suggestion else None,
                    dec.resume_mode.value if dec.resume_mode else None,
                    dec.rationale,
                    dec.created_at,
                ),
            )

    def decisions_for(self, task_id: str) -> list[DecisionRecord]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM decisions WHERE task_id=%s ORDER BY created_at", (task_id,)
            )
            rows = cur.fetchall()
        return [
            DecisionRecord(
                id=r["id"],
                task_id=r["task_id"],
                trigger=r["trigger_signal_ids"],
                decider=Decider(r["decider"]),
                complexity_score=r["complexity_score"],
                escalation_reason=r["escalation_reason"],
                action=Action(r["action"]),
                new_spec=TaskSpec.from_dict(r["new_spec_json"]) if r["new_spec_json"] else None,
                spec_changes=r["spec_changes_json"] or {},
                suggestion=r["suggestion_json"],
                resume_mode=ResumeMode(r["resume_mode"]) if r["resume_mode"] else None,
                rationale=r["rationale"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- events（M6 §9 第 4 条）-------------------------------------------- #

    def append_event(self, ev: TaskEvent) -> TaskEvent:
        """seq 在库里分配：并行调度下多个 Orchestrator 同时写，调用方自己算必然撞号。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE task_id=%s",
                (ev.task_id,),
            )
            ev.seq = cur.fetchone()["m"] + 1
            cur.execute(
                """INSERT INTO events (id, task_id, seq, kind, text, ref_id,
                                       payload_json, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    ev.id, ev.task_id, ev.seq, ev.kind, ev.text, ev.ref_id,
                    json.dumps(ev.payload), ev.created_at,
                ),
            )
        return ev

    def events_for(self, task_id: str, after_seq: int = 0) -> list[TaskEvent]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM events WHERE task_id=%s AND seq>%s ORDER BY seq",
                (task_id, after_seq),
            )
            rows = cur.fetchall()
        return [
            TaskEvent(
                id=r["id"], task_id=r["task_id"], seq=r["seq"], kind=r["kind"],
                text=r["text"], ref_id=r["ref_id"], payload=r["payload_json"] or {},
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- artifacts --------------------------------------------------------- #

    def save_artifact(self, art: Artifact) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO artifacts (id, task_id, kind, content_ref, summary, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET summary=EXCLUDED.summary""",
                (art.id, art.task_id, art.kind, art.content_ref, art.summary, art.created_at),
            )

    def load_artifact(self, artifact_id: str) -> Artifact | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM artifacts WHERE id=%s", (artifact_id,))
            r = cur.fetchone()
        return Artifact.from_dict(r) if r else None

    def close(self) -> None:
        self.conn.close()
