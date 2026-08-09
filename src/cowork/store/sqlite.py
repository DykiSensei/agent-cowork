"""SQLite 实现。表结构与 schema.sql 一一对应，只是 JSONB -> TEXT。

用途：跑通链路、跑测试、本地 demo。生产切 PostgresStore。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
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

DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT,
    revision        INTEGER NOT NULL,
    spec_json       TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    agent_id        TEXT,
    current_step    INTEGER NOT NULL DEFAULT 0,
    checkpoint_id   TEXT,
    interrupt_count INTEGER NOT NULL DEFAULT 0,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    started_at      REAL,
    artifacts_json  TEXT    NOT NULL DEFAULT '[]',
    signals_json    TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    step         INTEGER NOT NULL,
    context_json TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    level        TEXT NOT NULL,
    type         TEXT NOT NULL,
    source       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    raw_evidence TEXT,
    disposition  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    consumed_at  REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    trigger_signal_ids TEXT NOT NULL,
    decider            TEXT NOT NULL,
    complexity_score   REAL,
    escalation_reason  TEXT,
    action             TEXT NOT NULL,
    new_spec_json      TEXT,
    resume_mode        TEXT,
    rationale          TEXT NOT NULL,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_task ON signals(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ckpt_task ON checkpoints(task_id, step);
CREATE INDEX IF NOT EXISTS idx_dec_task ON decisions(task_id, created_at);
"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _synchronized(fn):
    """把整个方法体（含 fetch）放进同一把锁。

    M4 的并行调度让多个 Orchestrator 同时写同一个 store，而 sqlite3 连接不是
    线程安全的。串行化访问的代价可以接受（写入量很小），静默的数据竞争不可以。
    锁必须覆盖到 fetch —— 只锁 execute 的话，游标会在锁外回头碰连接。
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class SqliteStore:
    def __init__(self, path: str = ":memory:") -> None:
        # check_same_thread=False + 自己的锁：并行调度下连接必然跨线程使用，
        # 线程亲和性检查挡不住真正的问题，只会把它变成 ProgrammingError。
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.executescript(DDL)
        self.conn.commit()

    # -- tasks ------------------------------------------------------------- #

    @_synchronized
    def save_task(self, state: TaskState) -> None:
        self.conn.execute(
            """INSERT INTO tasks (id, parent_id, revision, spec_json, status, agent_id,
                                  current_step, checkpoint_id, interrupt_count,
                                  tokens_used, started_at, artifacts_json, signals_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 revision=excluded.revision, spec_json=excluded.spec_json,
                 status=excluded.status, agent_id=excluded.agent_id,
                 current_step=excluded.current_step, checkpoint_id=excluded.checkpoint_id,
                 interrupt_count=excluded.interrupt_count, tokens_used=excluded.tokens_used,
                 started_at=excluded.started_at, artifacts_json=excluded.artifacts_json,
                 signals_json=excluded.signals_json""",
            (
                state.spec.id,
                state.spec.parent_id,
                state.spec.revision,
                _dumps(state.spec.to_dict()),
                state.status.value,
                state.agent_id,
                state.current_step,
                state.checkpoint_id,
                state.interrupt_count,
                state.tokens_used,
                state.started_at,
                _dumps(state.artifacts),
                _dumps(state.signal_log),
            ),
        )
        self.conn.commit()

    def _row_to_task(self, r: sqlite3.Row) -> TaskState:
        return TaskState(
            spec=TaskSpec.from_dict(json.loads(r["spec_json"])),
            status=TaskStatus(r["status"]),
            agent_id=r["agent_id"],
            current_step=r["current_step"],
            checkpoint_id=r["checkpoint_id"],
            interrupt_count=r["interrupt_count"],
            artifacts=json.loads(r["artifacts_json"]),
            signal_log=json.loads(r["signals_json"]),
            tokens_used=r["tokens_used"],
            started_at=r["started_at"],
        )

    @_synchronized
    def load_task(self, task_id: str) -> TaskState | None:
        r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(r) if r else None

    @_synchronized
    def list_tasks(self) -> list[TaskState]:
        rows = self.conn.execute("SELECT * FROM tasks").fetchall()
        return [self._row_to_task(r) for r in rows]

    # -- checkpoints ------------------------------------------------------- #

    @_synchronized
    def save_checkpoint(self, cp: Checkpoint) -> str:
        self.conn.execute(
            "INSERT INTO checkpoints (id, task_id, step, context_json, created_at) VALUES (?,?,?,?,?)",
            (cp.id, cp.task_id, cp.step, _dumps(cp.agent_context.to_dict()), cp.created_at),
        )
        self.conn.commit()
        return cp.id

    @_synchronized
    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        r = self.conn.execute(
            "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        if not r:
            return None
        ctx = AgentContext.from_dict(json.loads(r["context_json"]))
        return Checkpoint(
            id=r["id"],
            task_id=r["task_id"],
            step=r["step"],
            agent_context=ctx,
            artifacts=[a.id for a in ctx.produced],
            created_at=r["created_at"],
        )

    # -- signals ----------------------------------------------------------- #

    @_synchronized
    def save_signal(self, sig: Signal) -> None:
        self.conn.execute(
            """INSERT INTO signals (id, task_id, level, type, source, payload_json,
                                    raw_evidence, disposition, created_at, consumed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 disposition=excluded.disposition, consumed_at=excluded.consumed_at""",
            (
                sig.id,
                sig.task_id,
                sig.level.value,
                sig.type.value,
                sig.source.value,
                _dumps(sig.payload),
                sig.raw_evidence,
                sig.disposition.value,
                sig.created_at,
                sig.consumed_at,
            ),
        )
        self.conn.commit()

    @_synchronized
    def signals_for(self, task_id: str) -> list[Signal]:
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [
            Signal.from_dict(
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "level": r["level"],
                    "type": r["type"],
                    "source": r["source"],
                    "payload": json.loads(r["payload_json"]),
                    "raw_evidence": r["raw_evidence"],
                    "disposition": r["disposition"],
                    "created_at": r["created_at"],
                    "consumed_at": r["consumed_at"],
                }
            )
            for r in rows
        ]

    # -- decisions --------------------------------------------------------- #

    @_synchronized
    def save_decision(self, dec: DecisionRecord) -> None:
        self.conn.execute(
            """INSERT INTO decisions (id, task_id, trigger_signal_ids, decider,
                                      complexity_score, escalation_reason, action,
                                      new_spec_json, resume_mode, rationale, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                dec.id,
                dec.task_id,
                _dumps(dec.trigger),
                dec.decider.value,
                dec.complexity_score,
                dec.escalation_reason,
                dec.action.value,
                _dumps(dec.new_spec.to_dict()) if dec.new_spec else None,
                dec.resume_mode.value if dec.resume_mode else None,
                dec.rationale,
                dec.created_at,
            ),
        )
        self.conn.commit()

    @_synchronized
    def decisions_for(self, task_id: str) -> list[DecisionRecord]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                DecisionRecord(
                    id=r["id"],
                    task_id=r["task_id"],
                    trigger=json.loads(r["trigger_signal_ids"]),
                    decider=Decider(r["decider"]),
                    complexity_score=r["complexity_score"],
                    escalation_reason=r["escalation_reason"],
                    action=Action(r["action"]),
                    new_spec=(
                        TaskSpec.from_dict(json.loads(r["new_spec_json"]))
                        if r["new_spec_json"]
                        else None
                    ),
                    resume_mode=ResumeMode(r["resume_mode"]) if r["resume_mode"] else None,
                    rationale=r["rationale"],
                    created_at=r["created_at"],
                )
            )
        return out

    # -- artifacts --------------------------------------------------------- #

    @_synchronized
    def save_artifact(self, art: Artifact) -> None:
        self.conn.execute(
            """INSERT INTO artifacts (id, task_id, kind, content_ref, summary, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET summary=excluded.summary""",
            (art.id, art.task_id, art.kind, art.content_ref, art.summary, art.created_at),
        )
        self.conn.commit()

    @_synchronized
    def load_artifact(self, artifact_id: str) -> Artifact | None:
        r = self.conn.execute(
            "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        return Artifact.from_dict(dict(r)) if r else None

    @_synchronized
    def close(self) -> None:
        self.conn.close()
