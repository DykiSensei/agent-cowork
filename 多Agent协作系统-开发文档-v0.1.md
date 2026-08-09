# 多 Agent 协作系统 — 开发文档 v0.4

> 状态：**M1 完成 3/4**，1.3 待模型 API key
> 日期：2026-08-10
>
> **v0.4 变更**：§10.3 补充多供应商实测结论（DeepSeek / Kimi 可用）与预算拒绝的错误形态；§10.4 沙箱隔离方案定稿；新增 §10.6 密钥与配置；§9 新增风险 #8；§11 按 M1 实测结果重写；§12 M1 标注进度。
> **v0.3 变更**：新增 §11 实现现状（文档 ↔ 代码对照）与 §12 开发路线图；§9 风险表按原型实测结果更新状态。
> **v0.2 变更**：新增 §10 技术栈决策；§4.1 TaskSpec 增加 `task_class` / `hard_signals` / `silence_policy` / `model` 字段；§3.2 补充信号覆盖面差异说明；风险 #4 给出缓解方案。

**原型结论（2026-08-10）**：核心架构假设成立。§10.1 的判断得到验证——自持 step 循环后，「外部抢占」确实退化成循环开头的一次状态检查（`runtime/loop.py:128`），没有引入任何框架依赖。`L0 信号 → 中断 → 架构师决策 → REBASE → 恢复 → 验收通过`全链路跑通。

**M1 阶段结论（2026-08-10）**：Postgres、Docker 沙箱、virtual key 预算强制三项已在真实环境验证，64 个测试通过。两个值得记的发现：**LiteLLM 的预算拒绝用 HTTP 429，与真实限流同码**，靠状态码判断必然误判（§10.3）；**Docker 沙箱原实现不满足出口标准**，`run` 能绕过工具层白名单，已改为只读挂载 + scope 覆盖（§10.4）。

---

## 1. 目标与范围

### 1.1 产品形态

用户在群聊中发布任务。架构师（Architect）拆解任务、派发给多个 Subagent 并行执行。执行过程中，Subagent 遇到问题会产生信号；架构师据此中断、修改任务、恢复执行。人可随时介入。

### 1.2 本文覆盖

- 角色划分与职责边界
- 信号协议（分级定义）
- 核心数据结构
- 中断 → 改任务 → 恢复的状态机
- 上下文取舍规则
- LLM/人的升级边界

### 1.3 本文不覆盖

技术选型、具体实现代码、部署方案、权限与鉴权设计、计费。

### 1.4 设计前提

本设计基于三条已有的经验性结论，后续所有取舍都回溯到这里：

| 结论 | 来源 | 对本设计的约束 |
|---|---|---|
| 无中心的并行 agent 错误放大 **17.2x**，有 orchestrator 时降到 **4.4x** | Scaling Agent Systems (arXiv 2512.08296) | **执行层必须中心化**。Subagent 之间不允许直接通信 |
| 失败构成：规格不清 42% / 协调崩溃 37% / 验证不足 21% | MAST (arXiv 2503.13657) | TaskSpec 必须结构化且带验收标准；信号协议必须显式定义 |
| 顺序依赖强的任务，多 agent 相对单 agent 最差 **−70%** | 同上 | 拆解时必须评估可分解性，不可分解就退化为单 agent |

**核心架构决策**：群聊是**界面层**，负责可观测性和人的介入；**执行层是中心化的**，Subagent 只与架构师通信。二者不可混淆。

---

## 2. 角色划分

```
┌─────────────────────────────────────────────┐
│  ChatSurface（群聊界面层）                    │
│  - 任务 thread 展示                          │
│  - 人的介入入口                              │
│  - DecisionRecord 可见化                     │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Architect（架构师，单一实例）                 │
│  - 持有连续上下文                             │
│  - 任务拆解 / 中断决策 / 改任务 / 验收         │
│  - LLM 自动 + 人升级                          │
└──────────────────┬──────────────────────────┘
                   │  仅此一条通信链路
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   ┌────────┐ ┌────────┐ ┌────────┐
   │Subagent│ │Subagent│ │Subagent│   ← 彼此不通信
   └───┬────┘ └───┬────┘ └───┬────┘
       │          │          │
┌──────▼──────────▼──────────▼───────────────┐
│  Runtime / Harness（确定性，不含 LLM）        │
│  - 观测执行、产生硬信号                       │
│  - checkpoint 持久化                         │
│  - 预算与超时控制                             │
└─────────────────────────────────────────────┘
```

### 2.1 Runtime / Harness

**不含任何 LLM**。这是整个设计里唯一完全可信的组件，所有硬信号都由它产生。

职责：

- 执行 Subagent 的每个 step，在 step 边界写 checkpoint
- 观测工具调用结果、退出码、耗时、token 消耗
- 强制超时与预算上限
- 校验 Subagent 输出是否符合 TaskSpec 声明的 schema
- 拦截越权操作（访问 TaskSpec.scope 之外的资源）

### 2.2 Subagent

临时、任务隔离、可并行。**不持有跨任务的记忆**，每次派发时由架构师注入完整上下文。

- 只能与架构师通信，产生的信号一律经架构师中转
- 可主动发送软信号，但**无权要求立即中断**
- 完成后返回结构化产出 + 摘要

### 2.3 Architect

单一实例，持有连续上下文，是唯一的写入决策点。

- 任务拆解与可分解性评估
- 消费信号、做中断决策
- 生成新的 TaskSpec、决定恢复模式
- 验收 Subagent 产出
- 判断是否升级给人

### 2.4 人

- 可在任何时刻通过群聊介入
- **人的介入视同硬信号**，具有最高优先级，立即抢占
- 对所有 DecisionRecord 有可见性（见 §7.3）

---

## 3. 信号协议

### 3.1 分级原则

信号分两级，**来源不同、可信度不同、处理方式不同**。这是本设计最关键的部分。

| | L0 硬信号 | L1 软信号 |
|---|---|---|
| 产生者 | Runtime（确定性检测）/ 人 | Subagent（自我判断） |
| 可信度 | 高 —— 是客观事实 | 低 —— agent 常意识不到自己跑偏 |
| 处理方式 | **抢占式**，立即中断 | **入队**，架构师在检查点批量消费 |
| 是否经 LLM 判断 | 否，直接触发 | 是，先做廉价评估 |

**为什么必须分开**：Subagent 自报有两个方向的错误——该报的没报（盲区），不该报的乱报（噪音）。如果把自报当硬信号，噪音会让架构师疲于奔命；如果完全不听，写作、调研这类没有客观判据的任务就彻底失去可观测性。分级是唯一能同时拿到两者价值的做法。

### 3.2 L0 硬信号清单

由 Runtime 检测，**无条件触发中断**，不经 LLM 判断：

| 信号 | 触发条件 |
|---|---|
| `TOOL_FAILURE` | 工具调用非零退出 / 抛异常 |
| `VALIDATION_FAILED` | 产出不符合 TaskSpec.output_schema |
| `TEST_FAILED` | 关联的验证命令失败 |
| `TIMEOUT` | 超过 TaskSpec.deadline（wall clock）|
| `STEP_LIMIT` | 超过 TaskSpec.max_steps |
| `BUDGET_EXCEEDED` | token 消耗超过 TaskSpec.token_budget |
| `SCOPE_VIOLATION` | 尝试访问 TaskSpec.scope 之外的资源 |
| `HUMAN_INTERVENTION` | 人在群聊中介入 |

> 设计注记：`SCOPE_VIOLATION` 兼作安全边界和跑偏探测器。Subagent 开始碰不该碰的东西，通常意味着它已经偏离了任务理解。

### 3.2.1 信号覆盖面因任务类型而异（重要）

上表不是全局统一适用的。不同 `task_class` 能产生的硬信号差别极大：

| task_class | 可用硬信号 | 覆盖情况 |
|---|---|---|
| `CODE` | 全部 8 条 | 密集且客观 |
| `TOOL_CALL` | `TOOL_FAILURE` / `VALIDATION_FAILED` / `SCOPE_VIOLATION` / 三条资源类 | 中等 |
| `GENERATIVE` | 仅 `TIMEOUT` / `STEP_LIMIT` / `BUDGET_EXCEEDED` | **稀疏，几乎无内容层判据** |

**这带来一个隐蔽的失败模式**：如果硬信号清单统一，架构师无法区分

- 「这个 Subagent 没发信号，是因为一切正常」
- 「这个 Subagent 没发信号，是因为它压根产生不了信号」

后者是**伪装成健康的失败**，比显式报错危险得多。

解法是 `TaskSpec.silence_policy`（见 §4.1）：

- `TRUST` —— 无信号即视为正常推进。适用于 `CODE` / `TOOL_CALL`
- `PROBE` —— 架构师按固定间隔主动索要中间产出并做验收，不等上报。**`GENERATIVE` 类必须用此模式**

`PROBE` 本质上是用主动轮询换取观测能力的缺失，token 成本明显更高。这是没有客观判据的任务必须付的代价，不要试图省掉。

### 3.3 L1 软信号清单

由 Subagent 主动上报，**进入队列，不立即中断**：

| 信号 | 语义 |
|---|---|
| `AMBIGUITY` | 任务描述存在歧义，需要澄清 |
| `ASSUMPTION_BROKEN` | 任务的前提假设被发现不成立 |
| `CONFLICT_SUSPECTED` | 怀疑与其他任务的产出冲突 |
| `RESOURCE_NEEDED` | 需要额外的工具/权限/信息 |
| `PROGRESS` | 阶段性进度汇报（纯信息，不触发评估）|

### 3.4 软信号的消费

架构师**不为每条软信号做完整推理**（成本会失控）。流程：

```
软信号入队
   ↓
到达消费检查点（下述任一）：
   - 任一 Subagent 完成一个 step
   - 队列长度 ≥ N
   - 距上次消费超过 T 秒
   ↓
廉价评估（小模型 / 低成本调用）
   批量读取队列，只输出：ignore | escalate
   ↓
   ├─ ignore  → 记入 log，不打断
   └─ escalate → 交由架构师主模型做完整中断决策
```

`CONFLICT_SUSPECTED` 例外：因为跨任务冲突是架构师的独有视野（Subagent 之间不通信，只有架构师能看到全局），此类信号**直接升级**，不走廉价评估。

---

## 4. 核心数据结构

以下为逻辑结构，字段名待实现时定稿。

### 4.1 TaskSpec

派发给 Subagent 的任务定义。**MAST 里 42% 的失败源于规格不清，所以这个结构必须强制填写验收标准**。

```
TaskSpec {
  id:              TaskId
  parent_id:       TaskId?          // 拆解自哪个任务
  revision:        int              // 每次改任务 +1

  goal:            string           // 目标，自然语言
  acceptance:      Criterion[]      // 验收标准，必填，至少一条
  output_schema:   Schema           // 产出结构，供 Runtime 校验

  // —— 任务类型与信号覆盖（见 §3.2.1）——
  task_class:      CODE | TOOL_CALL | GENERATIVE
  hard_signals:    SignalType[]     // 由 task_class 推导默认值，可显式覆盖
  silence_policy:  TRUST | PROBE    // 无信号时信任还是主动探查
  probe_interval:  duration?        // silence_policy=PROBE 时必填

  // —— 执行配置 ——
  model:           ModelId          // LiteLLM 模型标识，实现"不同模型干擅长的事"
  sandbox:         SandboxProfile?  // task_class=CODE 时必填
  scope:           ResourcePattern[] // 允许访问的资源，超出即 SCOPE_VIOLATION
  tools:           ToolId[]

  deadline:        duration
  max_steps:       int
  token_budget:    int

  context_refs:    ArtifactId[]     // 注入的上下文引用，非全文
  depends_on:      TaskId[]         // 顺序依赖，用于可分解性评估
}
```

**字段约束**：

- `acceptance` 必填是刻意的硬约束。写不出验收标准的任务，说明拆解本身没想清楚，不应该派发。
- `task_class = GENERATIVE` 时，`silence_policy` **强制为 `PROBE`**，由 Runtime 在构造时校验，架构师无权设为 `TRUST`。
- `model` 落到 LiteLLM 的 virtual key 上，同时承担 `token_budget` 的强制执行。

### 4.2 TaskState

```
TaskState {
  spec:            TaskSpec
  status:          PENDING | RUNNING | INTERRUPTED | AWAITING_HUMAN
                 | COMPLETED | FAILED | ABANDONED
  agent_id:        AgentId?
  current_step:    int
  checkpoint_id:   CheckpointId?    // 最近一次可恢复点
  interrupt_count: int              // 累计中断次数，见 §7.2
  artifacts:       ArtifactId[]     // 已产出的成果
  signal_log:      SignalId[]
}
```

### 4.3 Signal

```
Signal {
  id:              SignalId
  level:           L0 | L1
  type:            SignalType       // §3.2 / §3.3 清单
  task_id:         TaskId
  source:          RUNTIME | SUBAGENT | HUMAN
  payload:         object           // 类型相关的结构化数据
  raw_evidence:    string?          // 原始证据（stderr、失败输出等）
  created_at:      timestamp
  consumed_at:     timestamp?
  disposition:     PREEMPTED | ESCALATED | IGNORED | QUEUED
}
```

### 4.4 Checkpoint

```
Checkpoint {
  id:              CheckpointId
  task_id:         TaskId
  step:            int
  agent_context:   AgentContext     // 见 4.5
  artifacts:       ArtifactId[]     // 该点已完成的产出
  created_at:      timestamp
}
```

### 4.5 AgentContext

**关键设计**：显式区分「该保留的成果」和「该丢弃的过程」。这是恢复模式（§6）能工作的前提。

```
AgentContext {
  task_spec:       TaskSpec         // 当前任务定义
  injected:        Artifact[]       // 架构师注入的只读上下文
  produced:        Artifact[]       // ← 已产出成果，跨恢复保留
  reasoning_trace: Message[]        // ← 过程推理，REBASE 时丢弃
  summary:         string?          // produced 的压缩摘要
}
```

### 4.6 DecisionRecord

架构师的每一次中断决策都要留痕，群聊层据此渲染。

```
DecisionRecord {
  id:              DecisionId
  trigger:         SignalId[]       // 由哪些信号触发
  decider:         LLM | HUMAN
  complexity_score: float?          // LLM 自评，见 §7.1
  escalation_reason: string?        // 若升级给人，为什么
  action:          CONTINUE | MODIFY_TASK | ABANDON | REASSIGN
  new_spec:        TaskSpec?
  resume_mode:     RESUME | REBASE | RESTART
  rationale:       string           // 人可读的理由，群聊展示
  created_at:      timestamp
}
```

---

## 5. 关键流程：中断判定

```
                    ┌─────────────┐
                    │  RUNNING    │
                    └──────┬──────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   L0 硬信号           L1 软信号          正常完成
        │                  │                  │
        ▼                  ▼                  ▼
   立即抢占            入队等待          ┌──────────┐
   （不经 LLM）             │             │ 架构师验收│
        │                  ▼             └────┬─────┘
        │            到达检查点               │
        │                  │            ┌─────┴─────┐
        │                  ▼            │           │
        │            廉价批量评估      通过        不通过
        │                  │            │           │
        │           ┌──────┴──────┐     ▼           ▼
        │        ignore      escalate COMPLETED  当作 L0
        │           │             │                信号处理
        │           ▼             │
        │        记 log 继续       │
        │                         │
        └────────────┬────────────┘
                     ▼
              ┌─────────────┐
              │ INTERRUPTED │
              │  写checkpoint│
              └──────┬──────┘
                     ▼
              ┌─────────────┐
              │ 架构师决策   │  ← §7 升级边界在此判定
              └──────┬──────┘
                     │
        ┌────────┬───┴────┬──────────┐
        ▼        ▼        ▼          ▼
    CONTINUE  MODIFY  REASSIGN   ABANDON
        │      _TASK      │          │
        │        │        │          ▼
        │        ▼        │      ABANDONED
        │   选恢复模式     │
        │     （§6）      │
        └────────┴────────┘
                 ▼
            从 checkpoint 恢复
                 │
                 ▼
            ┌─────────┐
            │ RUNNING │
            └─────────┘
```

### 5.1 中断粒度

中断只发生在 **step 边界**。为保证「随时可打断」的体感，需要：

- Subagent 的执行被强制切分为 step，每个 step 结束写 checkpoint
- 单个 step 设 soft deadline；超过则 Runtime 强制切段，把当前状态落盘
- 长工具调用（如运行时间不可控的构建）单独成 step，中断时可选择等待完成或强杀

**取舍点**：step 粒度越细，中断响应越快，但 checkpoint 开销和状态同步成本上升。建议初版按「一次工具调用 = 一个 step」起步，实测后再调。

---

## 6. 关键流程：改任务与恢复

### 6.1 三种恢复模式

改了 TaskSpec 之后直接续跑是**错误的**——Subagent 的 `reasoning_trace` 里还留着旧任务的推理痕迹，模型会被旧目标带偏。

| 模式 | 保留 | 丢弃 | 适用 |
|---|---|---|---|
| `RESUME` | 全部上下文 | 无 | 任务未变，只是补充信息或重试瞬时故障 |
| `REBASE` | `produced` + `summary` + 新 TaskSpec | `reasoning_trace` | **默认**。TaskSpec 有实质变更 |
| `RESTART` | 无（可选保留 produced 作参考） | 全部 | 方向完全错误，成果不可用 |

### 6.2 选择规则

```
if spec.revision 未变:
    → RESUME
elif 仅 acceptance / scope / budget 变化，goal 未变:
    → REBASE
elif goal 实质变化 但 produced 仍有价值:
    → REBASE（produced 转为 injected 只读上下文）
else:
    → RESTART
```

### 6.3 REBASE 的执行

1. 从 checkpoint 取出 `AgentContext`
2. 对 `produced` 生成压缩摘要（这一步本身消耗 token，需计入预算）
3. 构造新的 `AgentContext`：
   - `task_spec` = 新的 TaskSpec（revision+1）
   - `injected` = 原 injected + produced 的摘要
   - `produced` = 保留原产出的引用
   - `reasoning_trace` = **清空**
4. 起新的 Subagent 实例执行

> 本质上，「修改任务」在实现上接近「杀掉重建，但保留产出」。这不是妥协，是必要的——避免旧目标污染是 REBASE 存在的全部理由。

---

## 7. 升级边界：LLM 与人

### 7.1 基本规则

LLM 自评复杂度，简单任务自己拍板，复杂任务升级给人。

```
complexity_score = LLM 对当前决策的自评（0.0 ~ 1.0）

if complexity_score < THRESHOLD:
    LLM 直接决策
else:
    升级给人
```

### 7.2 确定性下限（重要）

**纯靠 LLM 自评复杂度有盲区**：模型不知道自己不知道什么，它给出低分的场合恰恰可能是它没意识到问题严重性的场合。因此需要一组**不经 LLM 判断的无条件升级规则**作为兜底：

| 条件 | 理由 |
|---|---|
| 决策涉及不可逆操作（删除、部署、对外通信、支付） | 影响面不可回滚，与 LLM 的自信程度无关 |
| 同一 `task_id` 的 `interrupt_count` ≥ N（建议 N=3） | 反复中断说明 LLM 没找到根因，再让它试是浪费 |
| 决策会修改 `parent_id` 为空的顶层任务定义 | 触及用户原始意图 |
| 触发信号包含 `SCOPE_VIOLATION` | 已越界，需人确认边界是否该扩 |
| 累计 token 消耗超过任务预算的 X% | 成本失控 |

这几条与 `complexity_score` 是**或**的关系：任一命中即升级，LLM 无权覆盖。

### 7.3 可见性保证

要求：**每个任务对人都可见**。落实为三条：

1. 每个 TaskSpec 在群聊中对应一个 thread，状态变更实时反映
2. 每条 `DecisionRecord` 渲染为该 thread 中的一条消息，含 `rationale`、触发信号、恢复模式
3. LLM 自动决策的记录**与人的决策同等展示**，不折叠、不静默

人可在任意 thread 中介入 → 产生 `HUMAN_INTERVENTION` 硬信号 → 立即抢占。

> 可见性不等于要求人逐条阅读。默认全量展示、允许人按需过滤，好过默认折叠、需要人主动挖掘。

---

## 8. 上下文管理规则

| 规则 | 说明 |
|---|---|
| Subagent 上下文由架构师完全构造 | Subagent 无自主记忆，不跨任务持有状态 |
| `context_refs` 传引用不传全文 | 由 Runtime 按需解析，避免上下文膨胀 |
| Subagent 产出必须附摘要 | 架构师只读摘要做验收，需要时再拉全文 |
| 架构师上下文是唯一连续的 | 定期压缩，但不清空 |
| 跨任务信息只经架构师流转 | 禁止 Subagent 直接读取彼此产出 |

---

## 9. 已知风险与未决问题

| # | 问题 | 状态 | 归属里程碑 |
|---|---|---|---|
| 1 | step 粒度的经验值未定，需实测 checkpoint 开销 | 参数已收口到 `policy.step_soft_deadline_s`，默认 60s 为猜测值 | **M2** |
| 2 | 廉价评估用什么模型、`THRESHOLD` 取值 | 分诊模型已定（haiku，不开 thinking）；`complexity_threshold=0.6` 为猜测值 | **M2** |
| 3 | 架构师本身成为单点故障——它的规格拆解错误无人纠正 | **仍未解决**。原型未触及 | **M5** |
| 4 | 软信号在无客观判据的任务里是唯一可观测性来源 | 设计上已缓解（§3.2.1 强制 `PROBE`），但 PROBE 尚未实现 | **M3** |
| 5 | REBASE 的摘要压缩会丢信息，多次后累积失真 | 已加上限 `policy.max_rebase=3`，失真程度未实测 | **M2** |
| 6 | 群聊界面层与执行层的状态一致性 | 未设计 | **M6** |
| 7 | 并行 Subagent 产出冲突的检测与合并策略 | 未设计，当前仅靠 `CONFLICT_SUSPECTED` 被动发现 | **M4** |
| 8 | 架构师与 Subagent 共用 virtual key，预算耗尽会同时打掉决策能力 | M1.4 实测暴露。当前行为是挂起等人（正确但被动）；是否给架构师独立 key 未决 | **M2** |

### 关于风险 #3

架构师是本设计里唯一的写入决策点，也因此是唯一没有被验证的环节。MAST 数据里 42% 的失败来自规格不清——这个失败恰好发生在架构师这一层。当前设计对此没有防护，仅靠 §7.3 的可见性让人有机会发现。**这是 v0.1 最大的已知缺口。**

---

## 10. 技术栈决策

### 10.1 前置结论：step 循环必须自己持有

调研中的关键发现：**LangGraph 的 `interrupt()` 不是本设计需要的中断语义**。它是节点**内部**的暂停点——在节点里主动调用，图停在那一行等外部输入。它不支持从外部抢占一个正在运行的图。

这直接冲击 §3.2 的 L0 硬信号：Runtime 检测到测试失败要立即中断，但 Subagent 并没有停在任何 `interrupt()` 上等着。

**解法很便宜**：§5.1 已规定中断只发生在 step 边界。只要 step 循环是我们自己持有的，「外部抢占」就退化成「不派发下一个 step」——一次状态检查，不需要任何框架支持。

由此得出选型总原则：

> **控制流自己写，基础设施才外购。**

这条原则同时排除了 AutoGen 的 GroupChat 和 CrewAI 的 Process——它们替你决定「谁下一个发言」，而这恰恰是架构师的核心职责，不能外包。本设计的控制流不是静态图，是「架构师动态决策 + Runtime 派发」。

### 10.2 选型总表

| 层 | 选择 | 理由 | 何时该重新评估 |
|---|---|---|---|
| 语言 | **Python** | LiteLLM / 沙箱 SDK / 各家模型 SDK 均 Python 优先 | 界面层开工时考虑 TS |
| 编排 | **自写 step 循环** | 外部抢占的唯一可靠实现方式，见 §10.1 | 不会变，这是架构核心 |
| 持久化 | **Postgres 裸存** | 五张表即可，事务自控，心智负担低 | 需要跨天任务的崩溃恢复 / 复杂重试时，迁 DBOS |
| 模型路由 | **LiteLLM 自托管** | virtual key 承担 `token_budget` 强制执行，这是 §7.2 成本兜底的落地点 | 不会变 |
| 沙箱 | **本地 Docker** | v0.1 只需验证 `SCOPE_VIOLATION` 语义 | 规模化时选 E2B / Daytona / Modal |
| 界面 | **暂不做，CLI + 结构化日志** | 群聊界面验证不了任何架构假设 | 中断链路跑通后 |

### 10.3 关于模型路由

选 LiteLLM 而非 OpenRouter，**理由不是"支持多厂商"**（两者都支持），而是：

- `TaskSpec.token_budget` 和 §7.2 的成本兜底规则需要**按任务归集用量并强制上限**，这是 LiteLLM virtual key 的核心能力
- OpenRouter 更省事，但预算控制在它那一侧，我们拿不到执行权

早期探模型可以先用 OpenRouter，生产切自托管 LiteLLM 是常见路径。

#### 10.3.1 预算拒绝的错误形态（M1.4 实测）

virtual key 超预算时（litellm main-latest，2026-08）：

```
HTTP 429
{"error":{"message":"Budget has been exceeded! Key=… Current cost: 1.0, Max budget: 0.05",
          "type":"budget_exceeded","param":null,"code":"429"}}
```

**关键坑：预算拒绝与真实限流同为 429**，不能靠状态码判断，必须看错误体。优先匹配结构化的 `error.type`，文案串兜底，并要有反例护栏挡住 401 鉴权错误（实测 401 会带回上游原生错误体，里面不含预算字样）。

转换层落在 `llm/errors.py` 而非路线图假设的 `detectors.py`：后者是 Runtime 的确定性检测器，把 provider 错误分类混进去会污染「Runtime 不含 LLM」这条边界。

拒绝**发生在转发上游之前**，因此：不需要有效的上游 key 就能验证这条链路；SDK 对 429 的自动重试也不产生上游费用。

#### 10.3.2 供应商支持现状（M1.3 实测）

应用侧有两个后端，都实现同一个 `Backend` 协议：

| 后端 | 方言 | 覆盖供应商 | 状态 |
|---|---|---|---|
| `llm/anthropic_backend.py` | Anthropic Messages | Claude 全系 | 管道已验证到上游边界，待 key |
| `llm/openai_compat.py` | OpenAI Chat Completions | DeepSeek、Kimi(Moonshot)、任何 OpenAI 兼容端点 | 待 key |

**为什么需要第二个后端，而不是让 LiteLLM 翻译**：实测确认 LiteLLM 的 Anthropic 形状 `/v1/messages` 确实能路由到 DeepSeek/Moonshot（请求到了上游，回来的是各家自己的鉴权错误）。但我们依赖 `output_config.format`（Anthropic 专有的结构化输出），能否被忠实翻译成对方的 `response_format` 无法验证。**结构化输出被静默丢弃比不支持更糟**——Subagent 的动作解析会崩在一个看似正常的响应上。

所以 OpenAI 兼容后端直接说对方母语，并用三层保证 JSON 可靠性，不赌供应商的 schema 支持：

1. system prompt 里写死 schema，要求只输出 JSON
2. 支持时带 `response_format={"type":"json_object"}`（`deepseek-reasoner` 不支持，退化为纯提示词约束）
3. 本地用 Runtime 的 `validate_schema` 校验，不合格带着错误再问一轮；仍不合格则抛 `ModelCallFailed` → 硬信号

第 3 层是这个设计的要害：**校验权留在 Runtime 侧**，与「Runtime 不含 LLM 但负责确定性校验」是同一条原则。

两个后端都走 `llm/errors.py` 的同一套错误分类，所以 §10.3.1 验证过的预算强制对 DeepSeek/Kimi 同样成立。

### 10.4 关于沙箱：先不选型

有 `task_class = CODE` 的 Subagent 就必须有隔离，但 v0.1 用本地 Docker 足够验证语义。

#### 10.4.1 隔离方案（M1.2 实测后定稿）

**原实现不满足出口标准**：`-v {workspace}:/w` 把整个工作区可写挂载，`run` 执行任意代码即可绕过工具层的路径白名单。这不是理论风险——`test_local_sandbox_does_not_contain_run` 把它钉成了一条断言：本地模式下 `run` 确实改掉了 scope 外的文件。

定稿方案：

```
-v {workspace}:/w:ro                      # 整体只读
-v {workspace}/{scope 内路径}:/w/{同路径}   # 逐个可写覆盖
--network none
```

于是越权写入在**内核层面**被拒（`OSError: [Errno 30] Read-only file system`），Runtime 把这类失败提级为 `SCOPE_VIOLATION` 而非语焉不详的 `TOOL_FAILURE`。

两条实现约束：

- **只匹配「只读文件系统」这类明确的内核拒绝**，不匹配泛化的 `Permission denied`——后者会把应用自身的权限错误误判成越界
- bind mount 要求源文件已存在，所以无通配符的 scope 项若不存在会先建空文件。这些路径本就在 scope 内，创建它们不构成越界

由此得到一条一般性结论，对后续换 E2B / Daytona / Modal 同样适用：**工具层白名单管的是「我们提供的工具」，容器边界管的是「Subagent 能执行的一切」。前者是可用性设计，后者才是安全边界。**

三家的差异只在规模化时才值钱：

| | 隔离技术 | 冷启动 | 特点 |
|---|---|---|---|
| E2B | Firecracker microVM | ~150ms | 隔离最强，独立内核 |
| Daytona | 硬化 OCI 容器 | ~90ms | 冷启最快，适合每次工具调用起一个 |
| Modal | gVisor | 亚秒级 | 唯一支持沙箱内挂 GPU |

现在选等于提前锁死，且 `SandboxProfile` 已在 TaskSpec 里留了抽象位。

### 10.6 密钥与配置

§1.3 把「权限与鉴权设计」排除在外，这里只定最低操作基线——因为它影响的是代码结构，不定会渗进各处。

**载入顺序**：真实环境变量 > `.env` 文件。容器 / CI 用环境变量覆盖，本地用文件，互不打架。`.env` 的格式保持 docker compose 兼容（`KEY=value`，无 `export`、无 shell 展开），这样同一份文件既供 compose 做 `${VAR}` 替换，也供应用读取。只提交 `.env.example`。

**密钥不进三个地方**：

| 地方 | 措施 |
|---|---|
| 命令行参数 | CLI 不接受 key 参数——它会进 shell history 和进程列表 |
| 日志 | `load_env()` 只返回键名，不返回值 |
| 数据库 | `signals.raw_evidence` 存的是 provider 原始错误体，内容不受我们控制，在 `SignalBus.emit()` 这个唯一入口统一脱敏 |

第三条是唯一需要写代码的。它容易被忽略，因为脱敏的必要性来自一个间接链条：模型调用失败 → 错误体成为硬信号证据（§11.3c）→ 信号长期留在 Postgres。链条上任何一环单独看都不涉及密钥。

**沙箱与密钥**：容器只挂载任务 workspace，不挂载项目根目录，所以 `.env` 对 Subagent 不可见。这一条是 §10.4.1 只读挂载方案的附带收益，但值得显式记下来——如果将来为了方便把项目根挂进容器，就等于把所有密钥交给了 Subagent。

### 10.5 Postgres 表结构

```sql
tasks        (id, parent_id, revision, spec_json, status,
              agent_id, current_step, checkpoint_id, interrupt_count, ...)

checkpoints  (id, task_id, step, context_json, created_at)

signals      (id, task_id, level, type, source, payload_json,
              raw_evidence, disposition, created_at, consumed_at)

decisions    (id, trigger_signal_ids, decider, complexity_score,
              escalation_reason, action, new_spec_json, resume_mode,
              rationale, created_at)

artifacts    (id, task_id, kind, content_ref, summary)
```

**唯一不能将就的地方**：`checkpoints.context_json` 必须严格按 §4.5 把 `produced` 和 `reasoning_trace` 分成两个顶层键。REBASE 时直接丢弃后者、不做任何解析——这是恢复模式能低成本工作的全部前提。如果这里存成一坨扁平的消息列表，§6 整节都无法实现。

---

## 11. 实现现状（M1 阶段末）

### 11.1 文档 ↔ 代码对照

| 文档节 | 代码位置 | 状态 |
|---|---|---|
| §2 角色划分 | `runtime/` 无 LLM；`agent/architect.py`；`agent/subagent.py` | ✅ |
| §3.2 L0 硬信号（8 条） | `signals.py` + `runtime/detectors.py` + `runtime/loop.py` | ✅ 全部可产生 |
| §3.2.1 覆盖面分化 | `signals.default_hard_signals(task_class)` | ✅ |
| §3.3 L1 软信号 | `runtime/bus.py` 队列 | ✅ 入队 |
| §3.4 廉价分诊 | `agent/architect.py` | ⚠️ 逻辑在，未接真实小模型（M1.3） |
| §4 数据结构 | `types.py`（硬约束在 `__post_init__`） | ✅ |
| §5 中断状态机 | `orchestrator.py` | ✅ |
| §5.1 step 边界抢占 | `runtime/loop.py` `bus.take_preempt()` | ✅ **核心已验证** |
| §6 三种恢复模式 | `resume.py` | ✅ Postgres 上可直接查证 |
| §7.1 LLM 自评复杂度 | `agent/architect.py` | ✅ |
| §7.2 确定性升级下限 | `escalation.py` | ✅ 五条全实现 |
| §7.2 成本兜底（硬限制） | `llm/errors.py` + LiteLLM virtual key | ✅ **M1.4 实测** |
| §7.3 可见性 | `cli.py --json` 结构化日志 | ⚠️ 仅 CLI（M6） |
| §10.3 多供应商 | `llm/anthropic_backend.py` + `llm/openai_compat.py` | ⚠️ 管道通，待 key |
| §10.4 沙箱隔离 | `runtime/sandbox.py` 只读挂载 + scope 覆盖 | ✅ **M1.2 实测** |
| §10.5 五张表 | `schema.sql` + `store/postgres.py` | ✅ **M1.1 实测** |
| — | `store/sqlite.py`（零依赖，默认） | ✅ |

| §10.6 密钥与配置 | `config.py` + `.env.example` + `SignalBus.emit()` 脱敏 | ✅ |

79 个测试。不起 Docker 时依赖真实服务的 14 个 skip，其余照常跑。

### 11.2 四条架构不变量已有测试守护

| 不变量 | 测试 |
|---|---|
| Runtime 不含 LLM，硬信号全确定性产生 | `test_chain.test_signal_is_hard_and_preempting` |
| step 循环自持，抢占 = 不派发下一个 step | `test_preemption.py` |
| checkpoint 中 `produced` / `reasoning_trace` 分离 | `test_chain` + DB 层 CHECK 约束 |
| 执行层中心化，无 Subagent 间通信 API | 结构性保证（无对应接口） |

> DB 层用 `CHECK (context_json ? 'produced')` 把 §10.5 那条「唯一不能将就」的约束落到了数据库——写扁平消息列表会被直接拒绝，而不是等到 REBASE 时才发现。

### 11.3 原型暴露的两个设计补充

**（a）抢占队列必须清空**（`loop.py:110`）
中断时如果只取走触发的那一条硬信号，队列里剩余的会在下一轮循环开头再次触发抢占，**把一次中断放大成无限中断**。修正：中断时 `drain_preempt()` 全部取出并统一标记 `PREEMPTED`。文档 §5 未覆盖此细节，已在代码中处理。

**（b）模型不走工具调用循环**
Subagent 的模型调用用结构化输出直接返回「下一个动作」，而非让 SDK 托管工具循环。理由与 §10.1 同源：**循环必须归我们持有**，模型只提供决策数据。这条应视为 §10.1 原则的推论，对接任何模型 SDK 时都适用。

**（c）模型调用失败必须变成信号，不能变成异常**（M1 阶段补）
原实现里 Subagent 的模型调用一旦失败（鉴权、限流、代理拒绝预算），异常会一路抛穿整个 run——架构师连中断决策的机会都没有。这与 §5 的状态机是矛盾的：状态机假设任何失败都以信号形式进入决策流程。

修正：`llm/errors.py` 把 provider 错误归类成 `ModelError` 子类，每类携带一个 `signal_type`；step 循环捕获后发对应硬信号。由此得到一个此前没想到的边界情形——**Subagent 和架构师共用一把耗尽的 virtual key 时，两者会同时失效**。此时没有决策者，正确行为是挂起等人（`AWAITING_HUMAN`），而不是崩溃，也不是自作主张继续。已在 `test_budget_end_to_end` 中固化。

这暴露了一个设计层面的问题留给 M2 考虑：**架构师是否应该用独立的 virtual key**。共用一把 key 意味着 Subagent 烧完预算会连带打掉决策能力；分开则架构师至少还能做出「ABANDON / 升级给人」的决策。

### 11.4 剩余未测项

原来的三项已收口两项半：

| 项 | 状态 |
|---|---|
| `store/postgres.py` + `schema.sql` | ✅ 已实测（M1.1） |
| Docker 沙箱（`SandboxProfile.use_docker`） | ✅ 已实测，且实测后改了实现（M1.2 / §10.4.1） |
| 模型后端 | ⚠️ 管道验证到上游边界，**缺一把有效的 API key** |

「验证到上游边界」的含义：请求经官方 SDK → LiteLLM 代理 → 供应商 API，拿回了带 `request_id` 的原生响应，只在鉴权处失败。无效 key 不会让 run 崩，而是产出 `TOOL_FAILURE` 硬信号后挂起——这条路径本身已测。

未验证的是**模型能力相关的部分**：结构化输出的 schema 是否被各家如实遵守、架构师的裁决质量、opus/haiku（或 deepseek-chat/reasoner）的分工是否合理。这些只能用真实 key 跑出来。

`silence_policy=PROBE` 显式抛 `NotImplementedError`（`orchestrator.py`）而非半做——这是刻意的，见 M3。

---

## 12. 开发路线图

### 总览

```
M0 ✅ 核心链路验证        ← 已完成
M1 🔶 真实环境收口        ← 当前，3/4（1.3 待 key）
M2    参数实测            （依赖 M1：脚本后端上测出的参数没有意义）
M3    PROBE 模式          ┐
M4    并行与冲突检测      ┘ 可并行推进
M5    架构师验证          （风险 #3，最大缺口）
M6    群聊界面层          （与 M3–M5 无代码耦合，可独立启动）
```

**排序理由**：M2 必须在 M1 之后——用 `scripted` 后端测出的 `complexity_threshold` 和 step 耗时是自证的假数据。M5 需要 M2 的实测数据支撑，因为「架构师拆解质量」只有在参数稳定后才可归因。M6 唯一与产品价值直接相关，但验证不了任何架构假设，可交给独立的人并行做。

---

### M1 — 真实环境收口

**目标**：把 §11.4 的三项「已写未测」变成「已验证」。

| # | 任务 | 出口标准 | 状态 |
|---|---|---|---|
| 1.1 | 启动 Postgres，跑通 `test_postgres_store` | 3 个 skip 变 pass；两条 CHECK 约束验证生效（故意写扁平 context 应被拒） | ✅ |
| 1.2 | Docker 沙箱实测 | 越权访问真实触发 `SCOPE_VIOLATION`，而非仅工具层白名单拦截 | ✅ 实测后改了实现，见 §10.4.1 |
| 1.3 | 接 LiteLLM + 真实模型 | demo 场景用真实模型跑通 | ⏸ 待 key |
| 1.4 | virtual key 落地 `token_budget` | 超预算时 LiteLLM 侧真实拒绝，验证 §7.2 成本兜底不只是应用层软限制 | ✅ 见 §10.3.1 |

**1.3 的口径修正**：原出口写的是「架构师用 opus，分诊用 haiku」。实际无 Anthropic key，改为供应商无关——只要求 demo 场景在真实模型下跑通，具体分工按供应商能力定（DeepSeek 方案：架构师 `deepseek-reasoner`，Subagent / 分诊 `deepseek-chat`）。为此新增了 OpenAI 兼容后端，理由见 §10.3.2。

**里程碑出口**：demo 场景在「真实模型 + Postgres + Docker 沙箱」下完整跑通一次。
当前进度：Postgres + Docker 沙箱那半边已跑通（`demo --store pg --docker`），差真实模型。

**风险回顾**：1.4 的返工风险**确认存在**，且比预想的隐蔽——不是「映射不了」，而是**预算拒绝与真实限流同为 HTTP 429**，按状态码判断会把限流误判成预算耗尽。转换层已建，位置从 `detectors.py` 改到 `llm/errors.py`（理由见 §10.3.1）。

**M1 阶段新增的未预期工作**：
- OpenAI 兼容后端（§10.3.2）——原路线图假设只对接 Anthropic
- 模型调用失败的信号化（§11.3c）——原实现会让整个 run 崩

---

### M2 — 参数实测

**目标**：把 `policy.py` 里六个猜测值变成有依据的结论。

**前置**：需要先建一个 **10–20 个任务的固定任务集**（覆盖：一次通过 / 需一次 REBASE / 需多次 REBASE / 应升级给人）。没有任务集，所有参数都只能靠感觉。

| 参数 | 实测方法 | 关联风险 |
|---|---|---|
| `step_soft_deadline_s` | 测不同粒度下 checkpoint 写入开销占比与中断响应延迟 | #1 |
| `complexity_threshold` | 对照人工标注的「该不该升级」，调 ROC | #2 |
| `max_rebase` | 连续 REBASE 后对比产出与原始要求的偏离度 | #5 |
| `soft_queue_threshold` / `soft_interval_s` | 测分诊调用频次 × 单次成本 | #2 |
| `max_interrupts` | 统计中断次数与最终成功率的关系 | — |
| `budget_escalation_ratio` | 统计触发点距实际超支的距离 | — |

**里程碑出口**：每个参数有一句话的实测依据，写进 `policy.py` 注释。

**注意**：`complexity_threshold` 的调优需要人工标注样本，这是 M2 最费人力的部分，建议提前准备。

---

### M3 — PROBE 模式（`GENERATIVE` 类）

**目标**：解掉 `orchestrator.py:59` 的 `NotImplementedError`，让没有客观判据的任务可被观测。

| # | 任务 | 说明 |
|---|---|---|
| 3.1 | 实现按 `probe_interval` 主动索要中间产出 | 架构师发起，不等 Subagent 上报 |
| 3.2 | 中间产出的验收逻辑 | 复用 `acceptance` 中 `machine_checkable=false` 的标准，交模型判断 |
| 3.3 | 实测 PROBE 的 token 成本 | 与 `TRUST` 模式对比，量化「观测能力缺失的代价」 |
| 3.4 | 定 `probe_interval` 默认值 | 成本 vs 跑偏发现延迟 |

**里程碑出口**：一个 `GENERATIVE` 任务能跑通中断链路，且 PROBE 的成本溢价有明确数字。

**判断点**：如果 3.3 测出成本溢价过高（比如 >3x），需要回头重新考虑 §3.2.1 的设计——可能要引入「产出增量的确定性检查」（字数、结构完整性）作为廉价的伪硬信号，而不是全靠模型验收。

---

### M4 — 并行与冲突检测（风险 #7）

**目标**：从单任务扩展到混合 `task_class` 并行，这是你原始构想的形态。

| # | 任务 | 说明 |
|---|---|---|
| 4.1 | 多 Subagent 并行调度 | 注意仍不允许 Subagent 间通信，全部经架构师 |
| 4.2 | `depends_on` 的拓扑排序与可分解性评估 | 不可分解就退化为单 agent（§1.4 第三条约束） |
| 4.3 | 冲突检测从被动转主动 | 当前仅靠 `CONFLICT_SUSPECTED` 软信号；需增加产出层的确定性检查（如同一文件被多任务写入） |
| 4.4 | 冲突的合并策略 | 架构师仲裁，可能触发对某一分支的 REBASE |

**里程碑出口**：一个拆成 3–5 个子任务的复合任务跑通，其中至少含两种 `task_class`。

**注意**：4.3 是本阶段真正的难点。软信号靠不住这条结论在 §3.1 已确立，所以冲突检测**不能**只依赖 Subagent 自报怀疑。文件级写入冲突是最容易做的确定性检查，建议从这里起步。

---

### M5 — 架构师自身的验证（风险 #3）

**目标**：补上文档自评的最大缺口。架构师是唯一的写入决策点，也是唯一没被验证的环节——而 MAST 数据里 42% 的失败恰好发生在这一层。

候选方案（需先评估，不建议直接选定）：

| 方案 | 思路 | 代价 |
|---|---|---|
| 拆解后独立复核 | 顶层拆解完成后，用另一个模型/另一次调用做一致性检查 | 一次额外调用，但只在拆解时 |
| 验收标准反推 | 从 `acceptance` 反推「满足这些是否就等于完成 goal」 | 便宜，但只能查出明显疏漏 |
| 事后归因 | 任务失败后，回溯判断是拆解问题还是执行问题，累积成先验 | 无实时防护，但能改进后续拆解 |

**里程碑出口**：至少一种机制上线，且能在 M2 的任务集上验证出「原本会漏掉的拆解错误」。

**这是路线图里唯一没有确定解法的阶段**，做之前建议先用 M2 的任务集统计一下：失败案例中有多大比例真的归因于拆解？如果比例低，这个阶段可以后延。

---

### M6 — 群聊界面层（风险 #6）

**目标**：把 CLI + 结构化日志换成 §2 图里的 ChatSurface。

| # | 任务 | 说明 |
|---|---|---|
| 6.1 | 每个 TaskSpec 一个 thread | §7.3 第 1 条 |
| 6.2 | `DecisionRecord` 渲染为消息 | §7.3 第 2 条，**不折叠、不静默** |
| 6.3 | 人的介入入口 | 产生 `HUMAN_INTERVENTION` 硬信号，走既有抢占通道 |
| 6.4 | 执行层 → 界面层的状态同步 | 风险 #6 的正题 |

**里程碑出口**：人能在界面上看到一次完整的中断-改任务-恢复过程，并能主动打断。

**技术提示**：6.3 几乎是免费的——`bus.emit_hard(HUMAN_INTERVENTION)` 已经打通，界面层只需调用。6.4 才是真工作量：需要决定是轮询、SSE 还是 Postgres 的 `LISTEN/NOTIFY`（后者与已有存储层最贴合，无新组件）。

---

### 关于时间估算

本路线图**不给工期**。M1 是确定性工作，M2/M3 的时长取决于实测结果，M4/M5 存在方案返工的可能。建议按里程碑出口标准验收，而不是按日期。

如果要压缩范围先出一个可用版本：**M1 → M4 → M6** 是最短路径（真实环境 + 并行 + 界面），把 M2 的参数保留默认值、M3 只支持 `CODE` 和 `TOOL_CALL`、M5 后延。代价是接受未调优的参数和未验证的架构师——在内部试用阶段可以接受，对外则不行。

---

## 附：参考来源

- [Towards a Science of Scaling Agent Systems (arXiv 2512.08296)](https://arxiv.org/abs/2512.08296) — 架构与任务形状的匹配、错误放大倍数
- [Why Do Multi-Agent LLM Systems Fail? / MAST (arXiv 2503.13657)](https://arxiv.org/abs/2503.13657) — 失败分类学
- [LangChain: How and when to build multi-agent systems](https://www.langchain.com/blog/how-and-when-to-build-multi-agent-systems)
- [Durable execution 与 checkpoint 恢复](https://vadim.blog/durable-execution-agents-that-survive-failure-and-resume-where-they-left-off)
- [LangGraph 中断机制与外部抢占的限制](https://forum.langchain.com/t/how-can-i-implement-the-ability-to-interrupt-and-resume-execution-at-any-time/2356) — §10.1 的依据
- [Checkpoint 不等于 durable execution](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows) — 反方观点，迁 DBOS 时重读
- [LiteLLM vs OpenRouter 对比](https://www.truefoundry.com/blog/litellm-vs-openrouter)
- [Agent 沙箱对比：E2B / Modal / Daytona](https://www.developersdigest.tech/blog/ai-agent-code-sandbox-comparison-2026)
