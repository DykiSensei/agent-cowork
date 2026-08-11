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
  /**
   * 挂起的原因是「想跑一个不在白名单里的程序」时，是哪个程序。
   * 有值 = 界面该给一个「允许它并继续」的按钮，而不是让人去设置页改配置。
   * **来自信号的 payload，不是从理由文字里抠的** —— 后者会因为改一句话而
   * 失效，且失效方式是按钮悄悄不见了。
   */
  blocked_binary?: string | null;
}

/**
 * 拆解的实时进度。**全部是确定性的量，没有合成的百分比** ——
 * 轮数有上限但每轮耗时没有，百分比必然是编的。
 */
export interface PlanProgress {
  phase: "generating" | "reviewing";
  attempt: number;
  /** 分母是真的：重生成有 max_regenerate 上限。 */
  max_attempts: number;
  tokens: number;
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
  /** specs 是**复核前的草稿**（还会变）。终局的拆解没有这个标记。 */
  draft?: boolean;
  /** 拆解跑到哪一步了。只在 RUNNING 时有。 */
  progress?: PlanProgress | null;
  /** 服务端记的开始时刻（epoch 秒），用来算已用时间。 */
  started_at?: number;
  review?: ReviewData | null;
  /** 人裁决过没有、能不能派发。派发过一次之后 dispatched_root 非空。 */
  dispatchable?: boolean;
  ruling_note?: string;
  dispatched_root?: string | null;
  available_providers?: Record<string, string>;
  /** 产物落在哪。新任务是 <工作区>/<任务id>/，接手是工作区本身。 */
  workspace?: string;
  takeover?: boolean;
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

/**
 * 「这个任务此刻在做什么」（views.task_progress）。
 *
 * 只有确定性的东西：跑到第几步、烧了多少、最后一个动作是什么。
 * **动作是结构不是句子** —— 措辞归界面层（copy.ts），同一份数据在专业版和
 * 简洁版要说成两种话。
 */
export interface TaskProgress {
  task_id: string;
  goal: string;
  status: TaskStatus;
  terminal: boolean;
  revision: number;
  agent_id: string | null;
  current_step: number;
  max_steps: number;
  tokens_used: number;
  token_budget: number | null;
  scope: string[];
  /** 产物落在哪（spec.sandbox.workspace）。 */
  workspace: string;
  last_action: {
    step: number | null;
    kind: string | null;
    name: string | null;
    /** 对什么东西动手：路径或命令 */
    target: string;
    thought: string;
  } | null;
  last_result: {
    step: number | null;
    name: string | null;
    ok: boolean | null;
    exit_code: number | null;
    stderr: string;
  } | null;
  produced: string[];
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
  progress: TaskProgress;
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
  /**
   * 每个在等人的子任务，等的是什么。**复合线程上唯一的裁决入口** ——
   * 子任务折在父线程里，侧栏点不到它们。
   */
  pending: Record<string, PendingRuling | null>;
  /** 每个子任务此刻在做什么。 */
  progress: Record<string, TaskProgress>;
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

/** `GET /api/fs`：目录选择器的一层。 */
export interface FsListing {
  path: string;
  parent: string | null;
  entries: { name: string; path: string }[];
  /** true = 这是起点列表（主目录 / 盘符），不是某个真实目录的内容 */
  roots: boolean;
}

export interface Settings {
  base_url_override: string;
  models: { architect: string; subagent: string; triage: string };
  /**
   * 每个角色用哪一家（从已配 key 的供应商里选，空 = 自动）。
   * **和 models 是两件事**：那是模型 id 的覆盖，这是「谁来干」。
   * `reviewer` 多一个 `"none"` —— 明确关掉独立复核，退回同模型复核。
   */
  providers: { architect: string; reviewer: string; subagent: string };
  /** 默认工作区（产物落点的根）。空 = 用 workspace_default。 */
  workspace: string;
  /** 没配工作区时东西会落在哪 —— 只读，服务端算出来的。 */
  workspace_default: string;
  /** `run` 能调哪些可执行文件，逗号分隔。留空 = 各语言运行时。 */
  allowed_binaries: string;
  /** 一个子任务最多走几步。**"0" = 不限**。字符串，同 review_writes 那条理由。 */
  max_steps: string;
  /** 两个联网工具（fetch_url / search_web）的总闸（"on" / "off"）。**默认关**：
   * 取回的第三方内容会进 reasoning_trace 再进下一轮提示词，那是一条提示词注入通道。 */
  allow_network: string;
  /** 联网搜索（search_web）的状态。**只有 provider 是可写的**，key 只写不读。 */
  search: SearchSettings;
  /**
   * 三个角色的**附加**提示词（追加在内置提示词之后，不替换它）。
   * 内置提示词里带着输出契约和工具清单 —— 替换掉就是 100% 解析失败。
   */
  prompts: { architect: string; reviewer: string; subagent: string };
  effort: { architect: string; subagent: string; cheap: string };
  /**
   * 写入侧复核（§12 M8）。**字符串 "on" / "off"，不是布尔** ——
   * 它落到 .env，而空串在那里的语义是「未设置」→ 回落到默认（on），
   * 所以发 false 反而关不掉。服务端会拒非 on/off 的值。
   */
  review_writes: string;
}

/**
 * 联网搜索的配置与现状。
 *
 * 两把 key 的关系：**专用 key 优先，没有就用那家自己的**。所以已经配过
 * 智谱（模型供应商那一栏）的人，这里什么都不用填就能搜 —— `key_source`
 * 就是用来把这件事说清楚的，否则界面只会显示一个没有下文的「已配置」。
 */
export interface SearchSettings {
  /** 用户显式选的那家（空 = 用默认）。可写。 */
  provider: string;
  /** 实际生效的那家（provider 为空时是默认值）。只读。 */
  effective_provider: string;
  /** 可选的搜索供应商。 */
  options: string[];
  /** effective_provider 认不认识（配了个不存在的名字时为 false）。 */
  known: boolean;
  /** 那家自己的 key 变量名，例如 ZHIPUAI_API_KEY —— 界面要用它说「配哪个」。 */
  provider_key_env: string | null;
  /** 专用搜索 key 的变量名（COWORK_SEARCH_API_KEY）。 */
  dedicated_key_env: string;
  /** 这一刻能不能搜。false = search_web 不会进白名单，其余功能不受影响。 */
  configured: boolean;
  /** 用的是哪一把 key。null = 一把都没有。 */
  key_source: "dedicated" | "provider" | null;
  /** 末 4 位识别串。完整 key 永远不出服务端。 */
  key_hint: string | null;
}

/** `POST /api/search/test` 的结果。三种状态结论不同，不能都说成失败。 */
export interface SearchProbe {
  status: "ok" | "empty" | "failed";
  detail: string;
  sample?: { title: string; url: string };
}

// --------------------------------------------------------------------- //
// 前端内部的时间线模型（translate.ts 从 TaskDetail 翻出来）
// --------------------------------------------------------------------- //

export type StreamEvent =
  | { kind: "human"; text: string; ts: number | null }
  | { kind: "log"; text: string; ts: number | null; soft?: boolean }
  | { kind: "signal"; signal: Signal }
  | { kind: "decision"; decision: DecisionRecord }
  // taskId / title 只在复合线程上出现：那时候等人的是**某个子任务**，
  // 裁决要发给它而不是发给这条线程（root 根本没有 tasks 行）
  | {
      kind: "awaiting";
      pending: PendingRuling;
      ts: number | null;
      taskId?: string;
      title?: string;
    }
  | { kind: "plan"; plan: PlanData; tasks: Record<string, TaskState>; pendingChildren: string[] }
  | { kind: "terminal"; status: TaskStatus; title?: string; chips: string[] };
