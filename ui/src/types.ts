/**
 * 与 `cowork.views` 的投影形状对齐（M6-界面层接口.md §10）。
 * 后端源头：`src/cowork/types.py` 的 to_dict() + `src/cowork/views.py`。
 * fixtures 由 `ui/mock/make_fixtures.py` 重新生成，不要手改。
 */

export type TaskStatus =
  | "PENDING"
  | "RUNNING"
  | "INTERRUPTED"
  | "COMPLETED"
  | "AWAITING_HUMAN"
  | "FAILED"
  | "ABANDONED";

export interface Criterion {
  id: string;
  description: string;
  command: string[] | null;
}

export interface SandboxProfile {
  workspace: string;
  allowed_binaries: string[];
  use_docker: boolean;
  image: string;
  network: string;
}

export interface TaskSpec {
  id: string;
  parent_id: string | null;
  revision: number;
  goal: string;
  acceptance: Criterion[];
  output_schema: Record<string, unknown> | null;
  task_class: string;
  hard_signals: string[];
  silence_policy: string;
  probe_interval_s: number | null;
  model: string;
  sandbox: SandboxProfile | null;
  scope: string[];
  tools: string[];
  deadline_s: number | null;
  max_steps: number;
  token_budget: number | null;
  context_refs: string[];
  depends_on: string[];
}

export interface TaskState {
  task_id: string;
  parent_id: string | null;
  revision: number;
  goal: string;
  status: TaskStatus;
  agent_id: string | null;
  current_step: number;
  checkpoint_id: string | null;
  interrupt_count: number;
  artifacts: string[];
  signal_log: string[];
  tokens_used: number;
  started_at: number | null;
  spec: TaskSpec;
}

export interface Signal {
  id: string;
  level: "L0" | "L1";
  type: string;
  task_id: string;
  source: "RUNTIME" | "SUBAGENT" | "HUMAN";
  payload: Record<string, unknown>;
  raw_evidence: string | null;
  created_at: number;
  consumed_at: number | null;
  disposition: string;
}

/** 已生效的 spec 改动（§10.2）。画 diff 读这里，不用比对两版 spec。 */
export interface SpecChanges {
  goal?: string;
  added_criteria?: Criterion[];
}

/** 升级挂起时模型本来的意见（§10.1）：提议但未被采纳。 */
export interface Suggestion {
  action: DecisionRecord["action"];
  rationale: string;
  complexity_score: number | null;
  spec_changes: SpecChanges;
}

export interface DecisionRecord {
  id: string;
  task_id: string;
  trigger: string[];
  decider: "LLM" | "HUMAN";
  complexity_score: number | null;
  escalation_reason: string | null;
  action: "CONTINUE" | "MODIFY_TASK" | "ABANDON" | "REASSIGN";
  new_spec: TaskSpec | null;
  resume_mode: "RESUME" | "REBASE" | "RESTART" | null;
  rationale: string;
  created_at: number;
  spec_changes: SpecChanges;
  suggestion: Suggestion | null;
}

/** 时间线上的一条事件：到达序的索引，正文在 signals / decisions 里按 ref_id 查。 */
export interface TaskEvent {
  id: string;
  task_id: string;
  seq: number;
  kind: "human" | "log" | "signal" | "decision" | "status" | "plan" | "review";
  text: string;
  ref_id: string | null;
  payload: Record<string, unknown>;
  created_at: number;
}

/** 「这个任务此刻在等人吗、等的是什么」（views.pending_ruling）。 */
export interface PendingRuling {
  reason: string;
  suggestion: Suggestion | null;
  decision_id: string | null;
  checkpoint_id: string | null;
}

export interface PlanData {
  layers: string[][];
  max_parallel: number;
  decomposable: boolean;
  issues: { kind: string; detail: string; tasks: string[] }[];
}

export interface ReviewData {
  structural: { kind: string; detail: string; tasks: string[] }[];
  sufficient: boolean;
  missing: string[];
  tokens: number;
  reviewer: string;
  independent: boolean;
}

/**
 * `GET /api/plans/{id}`：一次拆解的现状（`DecompositionResult.to_dict()` +
 * 服务层补的几个字段）。拆解还在跑时只有 plan_id / goal / status。
 *
 * 三种终局与执行层同构：ACCEPTED ≙ COMPLETED，AWAITING_HUMAN ≙ AWAITING_HUMAN，
 * REJECTED ≙ ABANDONED。**都不是异常** —— 界面也要照这个分法渲染。
 */
export interface PlanView {
  plan_id: string;
  goal?: string;
  status: "RUNNING" | "ERROR" | "ACCEPTED" | "AWAITING_HUMAN" | "REJECTED";
  error?: string | null;
  root_id?: string | null;
  attempts?: number;
  tokens?: number;
  decider?: "LLM" | "HUMAN";
  escalation_reason?: string | null;
  rationale?: string;
  specs?: TaskSpec[];
  review?: ReviewData | null;
  /** 人裁决过没有、能不能派发。派发过一次之后 dispatched_root 非空。 */
  dispatchable?: boolean;
  ruling_note?: string;
  dispatched_root?: string | null;
  available_providers?: Record<string, string>;
}

/** views.thread_list() 的列表项。 */
export interface ThreadSummary {
  task_id: string;
  title: string;
  status: TaskStatus;
  composite: boolean;
  tokens_used: number;
  revision: number;
  current_step: number;
  terminal: boolean;
  updated_at: number | null;
}

interface DetailBase {
  events: TaskEvent[];
  signals: Record<string, Signal>;
  decisions: Record<string, DecisionRecord>;
}

export interface SingleDetail extends DetailBase {
  kind: "single";
  state: TaskState;
  pending: PendingRuling | null;
}

export interface CompositeDetail extends DetailBase {
  kind: "composite";
  state: null;
  /** 人最初说的那句话（root 线程第一条 human 事件）。老库 / CLI 入口没有则为空串。 */
  root_goal: string;
  plan: PlanData | null;
  review: ReviewData | null;
  tasks: Record<string, TaskState>;
  pending_children: string[];
}

/** GET /api/tasks/:id（views.task_detail）。 */
export type TaskDetail = SingleDetail | CompositeDetail;

// --------------------------------------------------------------------- //
// 设置页
// --------------------------------------------------------------------- //

export interface ProviderInfo {
  name: string;
  base: string | null;
  key_env: string;
  models: { subagent: string | null; architect: string | null; triage: string | null };
  /**
   * **「我们验证过这个预设」，不是「你的 key 有效」。**
   * 指的是 PROVIDERS 表里那一行的 model id 在本机用真 key 打通过。
   * 用户自己的 key 能不能用要看 ProbeResult —— 两件事共用一个标签的话，
   * 填完 key 看到「未验证」会以为是自己填错了。
   */
  preset_verified: boolean;
  /** @deprecated 用 preset_verified；这个只为兼容旧前端保留 */
  verified: boolean;
  effort: string | null;
  cache: string;
  /** 环境变量非空 —— 只说明「填了」，不说明「能用」 */
  configured: boolean;
  key_hint: string | null;
}

/** POST /api/providers/:name/test 的结果（后端 probe_provider）。 */
export interface ProbeResult {
  name: string;
  /**
   * ok         预设的 model id 都在服务端，这家现在能用
   * mismatch   端点通、key 有效，但预设写的 id 服务端没有（表过期了）
   * unreachable 问不到 —— **不代表配置错**
   * skipped    没有 key / 这家不吃 /v1/models —— **不代表配置错**
   */
  status: "ok" | "mismatch" | "unreachable" | "skipped";
  detail: string;
}

export interface Settings {
  base_url_override: string;
  models: { architect: string; subagent: string; triage: string };
  effort: { architect: string; subagent: string; cheap: string };
  /**
   * 写入侧复核（§12 M8）。**字符串 "on" / "off"，不是布尔** ——
   * 它落到 .env，而空串在那里的语义是「未设置」→ 回落到默认（on），
   * 所以发 false 反而关不掉。服务端会拒非 on/off 的值。
   */
  review_writes: string;
}

// --------------------------------------------------------------------- //
// 前端内部的时间线模型（translate.ts 从 TaskDetail 翻出来）
// --------------------------------------------------------------------- //

export type StreamEvent =
  | { kind: "human"; text: string; ts: number | null }
  | { kind: "log"; text: string; ts: number | null; soft?: boolean }
  | { kind: "signal"; signal: Signal }
  | { kind: "decision"; decision: DecisionRecord }
  | { kind: "awaiting"; pending: PendingRuling; ts: number | null }
  | { kind: "plan"; plan: PlanData; tasks: Record<string, TaskState>; pendingChildren: string[] }
  | { kind: "terminal"; status: TaskStatus; title?: string; chips: string[] };
