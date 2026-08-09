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
    TaskSpec,
    TaskState,
    TaskStatus,
)

DEFAULT_DSN = os.environ.get(
    "COWORK_PG_DSN", "postgresql://cowork:cowork@localhost:5433/cowork"
)


class PostgresStore:
    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        import psycopg  # 延迟导入：没装 psycopg 也能用 SqliteStore
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

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
                                          new_spec_json, resume_mode, rationale, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    dec.id,
                    dec.task_id,
                    json.dumps(dec.trigger),
                    dec.decider.value,
                    dec.complexity_score,
                    dec.escalation_reason,
                    dec.action.value,
                    json.dumps(dec.new_spec.to_dict()) if dec.new_spec else None,
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
                resume_mode=ResumeMode(r["resume_mode"]) if r["resume_mode"] else None,
                rationale=r["rationale"],
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
