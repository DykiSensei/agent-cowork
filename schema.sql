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
    -- 这次裁决改了哪些字段。只有 new_spec 的话，「哪条验收标准是这次新增的」
    -- 无法从存储重建（M6-界面层接口.md §9）。
    spec_changes_json  JSONB,
    -- 升级给人时 LLM 的建议 {action, rationale, complexity_score}。人还没答复的
    -- 那条记录里 action/rationale 记的是系统的兜底行为，不是模型的意见。
    suggestion_json    JSONB,
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

-- 时间线的到达序索引（M6 §9 第 4 条）。**不是内容的第二份拷贝**：
-- 信号与裁决的正文仍然只在各自的表里，这里只记「第几条、什么类型、指向谁」。
-- 排序靠 seq 不靠 created_at —— 并行任务的时间戳会撞在同一毫秒上。
--
-- **task_id 上刻意没有外键。** 事件是**线程级**的，而线程不等于任务：复合任务的
-- root 线程按设计就没有 tasks 行（见 views._synthetic_parent），可是分层结果、
-- 拆解复核、冲突仲裁全写在 root 上。加了外键这些写入会被拒绝，而调用方把异常
-- 吞掉（事件是旁路），结果是复合线程的时间线整个消失且不报错。别加回来。
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

CREATE INDEX IF NOT EXISTS idx_signals_task ON signals(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ckpt_task    ON checkpoints(task_id, step);
CREATE INDEX IF NOT EXISTS idx_dec_task     ON decisions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ev_task      ON events(task_id, seq);

-- 已有库的就地升级：加列必须幂等，否则换个版本再连老库就炸
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS spec_changes_json JSONB;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS suggestion_json   JSONB;
