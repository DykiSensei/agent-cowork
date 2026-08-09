-- 多 Agent 协作系统 v0.1 —— Postgres 裸存（开发文档 §10.5）
--
-- 唯一不能将就的地方：checkpoints.context_json 必须严格按 §4.5 把
-- produced 和 reasoning_trace 分成两个顶层键。REBASE 时直接丢弃后者、
-- 不做任何解析——这是恢复模式能低成本工作的全部前提。

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT REFERENCES tasks(id),
    revision        INTEGER     NOT NULL,
    spec_json       JSONB       NOT NULL,
    status          TEXT        NOT NULL
                    CHECK (status IN ('PENDING','RUNNING','INTERRUPTED','AWAITING_HUMAN',
                                      'COMPLETED','FAILED','ABANDONED')),
    agent_id        TEXT,
    current_step    INTEGER     NOT NULL DEFAULT 0,
    checkpoint_id   TEXT,
    interrupt_count INTEGER     NOT NULL DEFAULT 0,
    tokens_used     BIGINT      NOT NULL DEFAULT 0,
    started_at      DOUBLE PRECISION,
    artifacts_json  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    signals_json    JSONB       NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step         INTEGER NOT NULL,
    context_json JSONB   NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    -- 结构约束落到 DB 层：两个顶层键都必须在，写扁平消息列表直接被拒
    CONSTRAINT ctx_has_produced        CHECK (context_json ? 'produced'),
    CONSTRAINT ctx_has_reasoning_trace CHECK (context_json ? 'reasoning_trace')
);

CREATE TABLE IF NOT EXISTS signals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    level        TEXT NOT NULL CHECK (level IN ('L0','L1')),
    type         TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('RUNTIME','SUBAGENT','HUMAN')),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_evidence TEXT,
    disposition  TEXT NOT NULL
                 CHECK (disposition IN ('PREEMPTED','ESCALATED','IGNORED','QUEUED')),
    created_at   DOUBLE PRECISION NOT NULL,
    consumed_at  DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS decisions (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    trigger_signal_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    decider            TEXT  NOT NULL CHECK (decider IN ('LLM','HUMAN')),
    complexity_score   DOUBLE PRECISION,
    escalation_reason  TEXT,
    action             TEXT  NOT NULL
                       CHECK (action IN ('CONTINUE','MODIFY_TASK','ABANDON','REASSIGN')),
    new_spec_json      JSONB,
    resume_mode        TEXT CHECK (resume_mode IN ('RESUME','REBASE','RESTART')),
    rationale          TEXT  NOT NULL,
    created_at         DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_task ON signals(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ckpt_task    ON checkpoints(task_id, step);
CREATE INDEX IF NOT EXISTS idx_dec_task     ON decisions(task_id, created_at);
