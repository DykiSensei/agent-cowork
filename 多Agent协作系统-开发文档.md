# 多 Agent 协作系统 — 开发文档 v0.9

> 状态：**M5a / M5b 完成**，下一步 **M7（拆解三角色）**
> 日期：2026-08-10
>
> **v0.9 变更**：新增 §12 M7 —— 把架构师这一层拆成生成者 / 复核者 / 人，裁决规则定为「生成 → 复核 → 重生成 ≤N 次 → 升级给人」（与执行层的中断循环同构，复用 escalation / policy 而非新建）；§2.3 加 M7 前瞻注记并标明拆解生成侧未实现；§9 风险 #3 / #14 归口到 M7；路线图把 M7 排在 M6 之前。
> **v0.8 变更**：M5a / M5b 收口，新增 §11.9（停止判断）与 §11.10（拆解复核）；§7.2 新增两条确定性规则（决策无效、ABANDON 必升级）；`Backend` 新增 `review_decomposition`，`decide_interrupt` 增加 `history` 入参；§9 风险 #3 降级为「被削弱未消除」、新增 #14 / #15。
> **v0.7 变更**：M3 / M4 收口，新增 §11.7（PROBE 实测）与 §11.8（并行与冲突检测）；§3.2 硬信号增至 9 条（新增 `CONFLICT_DETECTED`）；§9 风险 #4 降级为「收益未标定」、#7 / #10 / #11 收口、新增 #12 / #13；§12 M5 按 M2 归因数据拆成 M5a / M5b。
> **v0.6 变更**：M2 收口，新增 §11.6 参数实测（75 次真实运行）；`policy.py` 六个参数全部带上实测依据；§9 风险 #1 证伪、#5 降级为「无样本」、新增 #10 / #11；§12 M2 标注完成并给 M4 加了两项。
> **v0.5 变更**：M1.3 收口，新增 §11.5 真实模型实测发现；§12 M2 补充「场景方差」前置条件；§9 新增风险 #9。
> **v0.4 变更**：§10.3 补充多供应商实测结论（DeepSeek / Kimi 可用）与预算拒绝的错误形态；§10.4 沙箱隔离方案定稿；新增 §10.6 密钥与配置；§9 新增风险 #8；§11 按 M1 实测结果重写；§12 M1 标注进度。
> **v0.3 变更**：新增 §11 实现现状（文档 ↔ 代码对照）与 §12 开发路线图；§9 风险表按原型实测结果更新状态。
> **v0.2 变更**：新增 §10 技术栈决策；§4.1 TaskSpec 增加 `task_class` / `hard_signals` / `silence_policy` / `model` 字段；§3.2 补充信号覆盖面差异说明；风险 #4 给出缓解方案。

**原型结论（2026-08-10）**：核心架构假设成立。§10.1 的判断得到验证——自持 step 循环后，「外部抢占」确实退化成循环开头的一次状态检查（`runtime/loop.py` 里的 `bus.take_preempt()`），没有引入任何框架依赖。`L0 信号 → 中断 → 架构师决策 → REBASE → 恢复 → 验收通过`全链路跑通。

**M1 阶段结论（2026-08-10）**：四项全部收口，测试全绿（Postgres + Docker + LiteLLM + DeepSeek/Kimi 全部在线，零 skip）。三个值得记的发现：

- **LiteLLM 的预算拒绝用 HTTP 429，与真实限流同码**，靠状态码判断必然误判（§10.3.1）
- **Docker 沙箱原实现不满足出口标准**，`run` 能绕过工具层白名单，已改为只读挂载 + scope 覆盖（§10.4.1）
- **demo 场景对真实模型失去了区分度**，原设计的「隐藏要求」被模型直接推断出来，三次运行零中断。场景已重新设计（§11.5）

**M2 阶段结论（2026-08-10）**：15 个任务 × 5 次 = 75 次真实运行、1.62M token，六个参数全部有了实测依据（§11.6）。三条比参数本身更重要：

- **LLM 自评复杂度判别力很弱**（AUC 0.672）。90 条「该升级」的决策里 63 条是被 §7.2 的确定性规则拦下的——**§7.2 才是升级边界的主力，§7.1 是补充**
- **架构师占掉总 token 的 51.3%**，是系统里最贵、最有权、且唯一无人复核的组件（风险 #3 因此更紧迫）
- **两个参数在当前调用路径上是死的**（`soft_queue_threshold` / `soft_interval_s`），一个假设被证伪（checkpoint 开销制约 step 粒度）。**实测的价值一半在于告诉你哪些参数根本不该存在**

**M3 / M4 阶段结论（2026-08-10）**：PROBE 与并行调度都已落地并在真实模型上跑通（§11.7 / §11.8）。两条值得记：

- **PROBE 的表面成本溢价 3.4x 是假的**，控制住中断次数后是 1.45x。差点因为一个没控变量的中位数比较去改 §3.2.1 的设计。**跨组比中位数时，高方差项没控住的话，中位数比较本身就是噪声**
- **PROBE 27 次探查 0 次判跑偏**：成本已知、收益未知。这是 §3.2.1 那句「这是没有客观判据的任务必须付的代价」目前最诚实的状态 —— 代价已量化，必要性还没有

**M5a / M5b 阶段结论（2026-08-10）**：架构师的停止判断和拆解复核都上线并实测（§11.9 / §11.10）。两条：

- **第一版 ABANDON 判据是一次真实的回归，只有对照组发现了它**。不可解任务上主动放弃从 12% 涨到 96% 看起来是大胜，可解任务的完成率同时从 81% 塌到 56%、`MULTI_REBASE` 归零 —— 它不是判别力变强，是**无差别放弃**。**提示词只能调偏置；要判别力必须让它先分辨证据的性质**。重写后 Youden J 从 0.12 到 0.60
- **确定性护栏只有在提示词不再走极端时才有事可做**：停滞判据的命中次数 0 → 1 → 17。它是兜底，不是主力 —— 这修正了我们原先「确定性规则是主力」的预期

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

> **M7 计划把这一层拆成三个角色**（§12 M7）：生成者（**有写权**）/ 复核者（无写权，只产出 findings）/ 人（仲裁）。
> **「唯一的写入决策点」这条不变，写权仍然只在生成者手上** —— 复核者是顾问。
> 当前代码里「任务拆解与可分解性评估」这一条**尚未实现**（风险 #14）：
> `Orchestrator` / `Scheduler` 拿到的都是现成的 `TaskSpec`。

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
| `TOOL_FAILURE` | 工具调用**任务级**失败（见下方注记）|
| `VALIDATION_FAILED` | 产出不符合 TaskSpec.output_schema |
| `TEST_FAILED` | 关联的验证命令失败 |
| `TIMEOUT` | 超过 TaskSpec.deadline（wall clock）|
| `STEP_LIMIT` | 超过 TaskSpec.max_steps |
| `BUDGET_EXCEEDED` | token 消耗超过 TaskSpec.token_budget |
| `SCOPE_VIOLATION` | 尝试访问 TaskSpec.scope 之外的资源 |
| `HUMAN_INTERVENTION` | 人在群聊中介入 |
| `CONFLICT_DETECTED` | 同层并行的两个任务写了同一份产出（M4 新增，§11.8c）|

> 设计注记：`SCOPE_VIOLATION` 兼作安全边界和跑偏探测器。Subagent 开始碰不该碰的东西，通常意味着它已经偏离了任务理解。

> **`TOOL_FAILURE` 的判据是「任务级失败」，不是「任何非零返回」**（M2/M3 实测修正，§11.6a / §11.6e）。两类失败明确排除在外，由 `ToolResult.hard_failure=False` 标记——结果照样回给模型，只是不抢占：
>
> - **探测性查询返回否定答案**。`read_file` 一个还不存在的文件是 Subagent 正常的第一步。原先这会让每个任务开局白烧一轮架构师决策。
> - **Subagent 主动预演验收命令**。自测是 step 循环内部的事；验收的判定权归 Runtime 在 `Finish` 之后行使，那时失败仍然产生 `TEST_FAILED`，覆盖面一条不少。原先这占了全部硬信号的 50%，典型形态是「Subagent 自测失败 → 抢占 → 架构师说『继续』→ Subagent 自己改对」，架构师什么信息都没提供却花掉约 3.5k token。
>
> 判据放宽的代价是真实的：**硬信号的噪声会淹没真正的越界**。

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
| 1 | step 粒度的经验值未定，需实测 checkpoint 开销 | ✅ **前提被证伪**（§11.6c）。checkpoint 写入占 step 耗时的 0.009%，粒度不受它制约。`step_soft_deadline_s` 定为 30s，但**当前无代码读它** | M2 ✅ |
| 2 | 廉价评估用什么模型、`THRESHOLD` 取值 | ⚠️ **测了，结论是这条路径没那么有用**。`complexity_threshold` 调到 0.4（最佳 Youden），但 AUC 仅 0.672；90 条该升级的决策里 63 条靠确定性规则拦下（§11.6c） | M2 ✅ |
| 3 | 架构师本身成为单点故障——它的规格拆解错误无人纠正 | ⚠️ **被削弱，未消除**。停止判断已修（§11.9，判别力 J 0.12→0.60，且任何 ABANDON 都升级给人）；拆解复核已上线并在 M4 场景上验证 10/10（§11.10）。**但复核者与拆解者是同一个模型** | M5 ⚠️ → **M7** 换独立模型 |
| 4 | 软信号在无客观判据的任务里是唯一可观测性来源 | ⚠️ **PROBE 已实现，但收益未标定**：27 次探查 0 次判跑偏，成本 1.45–1.64x 已知、收益未知（§11.7b）。软信号本身也极稀疏（75 次运行 20 条） | M3 ⚠️ |
| 5 | REBASE 的摘要压缩会丢信息，多次后累积失真 | ⚠️ **无样本，非已证伪**（§11.6c）。40 次完成的运行意图偏离 0 次，但 REBASE ≥3 次的完成样本只有 1 个。上限收到 2 | M2 ⚠️ |
| 6 | 群聊界面层与执行层的状态一致性 | 未设计 | **M6** |
| 7 | 并行 Subagent 产出冲突的检测与合并策略 | ✅ 已转主动：静态 scope 交集检测（派发前串行化）+ 产出层确定性检查 → 新增 L0 信号 `CONFLICT_DETECTED`；仲裁走既有 `Architect.decide()`（§11.8c/d） | M4 ✅ |
| 8 | 架构师与 Subagent 共用 virtual key，预算耗尽会同时打掉决策能力 | M1.4 实测暴露。当前行为是挂起等人（正确但被动）；是否给架构师独立 key **仍未决**——M2 未触及，因为实测中应用层预算一次都没真正打穿 | **M4** |
| 9 | 用脚本后端设计的任务集，放到真实模型上可能整体退化成「一次通过」 | ✅ 任务集已建（`bench/tasks.py`，15 个任务），四类形态均保住区分度。但暴露出新形态：验收命令对 Subagent 可执行时，失败信息本身会泄露隐藏要求（§11.6e） | M2 ✅ |
| 10 | L0 硬信号的判据过宽：任何非零返回都抢占 | ✅ 两处都已修：探测性 `read_file`（§11.6a）、Subagent 预演验收命令（§11.6e）。判据改为「任务级失败」而非「任何非零返回」，由 `ToolResult.hard_failure` 承载 | M4 ✅ |
| 11 | 工具面缺「列目录」，真实 agent 只能去调 `ls` 然后越界 | ✅ 已加 `list_files`。只读、不受 scope 限制（scope 限制的是写），仍受 workspace 边界限制 | M4 ✅ |
| 12 | PROBE 的收益未标定：探查有成本无战果 | M3 暴露（§11.7b）。需要一个会可靠漂移的 `GENERATIVE` 任务才能标定，而这类任务本身难设计（同 §11.5a） | **M5 之后** |
| 13 | 给循环加「让出控制权」时，以循环为计量单位的上限会被清零 | M3 踩到（§11.7d）：探查分段一度把 `max_steps` / `deadline_s` 打掉，而那正是 `GENERATIVE` 仅剩的硬信号。已修，但这是一类模式而非一个 bug | 已修，留作模式 |
| 14 | 架构师**不会拆解**——生成侧从未实现 | M5b 澄清（§11.10）。§2.3 把「任务拆解与可分解性评估」列为架构师职责，但 `Orchestrator` / `Scheduler` 拿到的都是现成的 TaskSpec。复核侧已上线，无对象可复核 | **M7**（7.3） |
| 15 | 提示词只能调偏置，判别力要靠证据分层 | M5a 踩到（§11.9c）：第一版 ABANDON 判据把架构师从「从不放弃」推成「无差别放弃」，可解任务完成率 81%→56%。**只有对照组能发现这件事** | 已修，留作模式 |

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
| `llm/anthropic_backend.py` | Anthropic Messages | Claude 全系 | 管道验证到上游边界，无 key 未跑通 |
| `llm/openai_compat.py` | OpenAI Chat Completions | DeepSeek、Kimi(Moonshot)、任何 OpenAI 兼容端点 | ✅ 完整链路已跑通（§11.5） |

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
| §3.2 L0 硬信号（9 条） | `signals.py` + `runtime/detectors.py` + `runtime/loop.py` + `scheduler.py` | ✅ 全部可产生 |
| §3.2.1 覆盖面分化 | `signals.default_hard_signals(task_class)` | ✅ |
| §3.3 L1 软信号 | `runtime/bus.py` 队列 | ✅ 入队 |
| §3.4 廉价分诊 | `agent/architect.py` | ⚠️ 逻辑在，未接真实小模型（M1.3） |
| §4 数据结构 | `types.py`（硬约束在 `__post_init__`） | ✅ |
| §5 中断状态机 | `orchestrator.py` | ✅ |
| §5.1 step 边界抢占 | `runtime/loop.py` `bus.take_preempt()` | ✅ **核心已验证** |
| §6 三种恢复模式 | `resume.py` | ✅ Postgres 上可直接查证 |
| §7.1 LLM 自评复杂度 | `agent/architect.py` | ⚠️ 实现完整，但 **M2 实测判别力弱**（AUC 0.672，§11.6c） |
| §7.2 确定性升级下限 | `escalation.py` | ✅ 五条全实现，**M2 显示它承担了 70% 的正类召回** |
| §7.2 成本兜底（硬限制） | `llm/errors.py` + LiteLLM virtual key | ✅ **M1.4 实测** |
| §7.3 可见性 | `cli.py --json` 结构化日志 | ⚠️ 仅 CLI（M6） |
| §10.3 多供应商 | `llm/anthropic_backend.py` + `llm/openai_compat.py` | ✅ **M1.3 实测**（DeepSeek / Kimi） |
| §10.4 沙箱隔离 | `runtime/sandbox.py` 只读挂载 + scope 覆盖 | ✅ **M1.2 实测** |
| §10.5 五张表 | `schema.sql` + `store/postgres.py` | ✅ **M1.1 实测** |
| — | `store/sqlite.py`（零依赖，默认） | ✅ |

| §10.6 密钥与配置 | `config.py` + `.env.example` + `SignalBus.emit()` 脱敏 | ✅ |
| §12 M2 参数实测 | `bench/tasks.py` + `bench/runner.py` + `bench/analyze.py` | ✅ **M2 实测**（§11.6） |
| §3.2.1 PROBE 模式 | `runtime/loop.py`（只判到点）+ `agent/architect.py` + `llm/*.probe()` | ✅ **M3 实测**（§11.7） |
| §12 M4 并行与冲突 | `plan.py`（确定性分层）+ `scheduler.py` | ✅ **M4 实测**（§11.8） |

147 个测试。不起 Docker 时依赖真实服务的 14 个 skip，其余照常跑。

### 11.2 四条架构不变量已有测试守护

| 不变量 | 测试 |
|---|---|
| Runtime 不含 LLM，硬信号全确定性产生 | `test_chain.test_signal_is_hard_and_preempting` |
| step 循环自持，抢占 = 不派发下一个 step | `test_preemption.py` |
| checkpoint 中 `produced` / `reasoning_trace` 分离 | `test_chain` + DB 层 CHECK 约束 |
| 执行层中心化，无 Subagent 间通信 API | 结构性保证（无对应接口） |

> DB 层用 `CHECK (context_json ? 'produced')` 把 §10.5 那条「唯一不能将就」的约束落到了数据库——写扁平消息列表会被直接拒绝，而不是等到 REBASE 时才发现。

### 11.3 原型暴露的两个设计补充

**（a）抢占队列必须清空**（`loop.py` 的 `interrupted()`）
中断时如果只取走触发的那一条硬信号，队列里剩余的会在下一轮循环开头再次触发抢占，**把一次中断放大成无限中断**。修正：中断时 `drain_preempt()` 全部取出并统一标记 `PREEMPTED`。文档 §5 未覆盖此细节，已在代码中处理。

**（b）模型不走工具调用循环**
Subagent 的模型调用用结构化输出直接返回「下一个动作」，而非让 SDK 托管工具循环。理由与 §10.1 同源：**循环必须归我们持有**，模型只提供决策数据。这条应视为 §10.1 原则的推论，对接任何模型 SDK 时都适用。

**（c）模型调用失败必须变成信号，不能变成异常**（M1 阶段补）
原实现里 Subagent 的模型调用一旦失败（鉴权、限流、代理拒绝预算），异常会一路抛穿整个 run——架构师连中断决策的机会都没有。这与 §5 的状态机是矛盾的：状态机假设任何失败都以信号形式进入决策流程。

修正：`llm/errors.py` 把 provider 错误归类成 `ModelError` 子类，每类携带一个 `signal_type`；step 循环捕获后发对应硬信号。由此得到一个此前没想到的边界情形——**Subagent 和架构师共用一把耗尽的 virtual key 时，两者会同时失效**。此时没有决策者，正确行为是挂起等人（`AWAITING_HUMAN`），而不是崩溃，也不是自作主张继续。已在 `test_budget_end_to_end` 中固化。

这暴露了一个设计层面的问题留给 M2 考虑：**架构师是否应该用独立的 virtual key**。共用一把 key 意味着 Subagent 烧完预算会连带打掉决策能力；分开则架构师至少还能做出「ABANDON / 升级给人」的决策。

### 11.4 剩余未测项

原来的三项全部收口：

| 项 | 状态 |
|---|---|
| `store/postgres.py` + `schema.sql` | ✅ 已实测（M1.1） |
| Docker 沙箱（`SandboxProfile.use_docker`） | ✅ 已实测，且实测后改了实现（M1.2 / §10.4.1） |
| 模型后端 | ✅ DeepSeek / Kimi 已跑通完整链路（M1.3 / §11.5）；Anthropic 路径验证到上游边界，无 key |

`silence_policy=PROBE` 已在 M3 实现（§11.7）。

### 11.5 真实模型实测发现（M1.3）

供应商：DeepSeek（架构师 `deepseek-reasoner`，Subagent / 分诊 `deepseek-chat`）与 Kimi（`kimi-k3`）。两家都跑通了完整链路，产出的 `solution.py` 经独立复核正确。

**（a）demo 场景对真实模型失去了区分度——已重新设计**

原场景把「需要归一化大小写与标点」当作 goal 里没写的隐藏要求。脚本后端靠脚本强制写出朴素实现，于是必然触发 `TEST_FAILED`。但真实模型**直接就写对了**：连续三次运行零中断，链路一次都没被触发。

这不是模型太强，是**场景设计有问题**：一个能被模型推断出来的「隐藏要求」，根本不是规格缺失。MAST 说的「规格不清 42%」指的是规格里**客观缺失**的部分，而不是没写全但可以合理补全的部分。

改成一条真正不可推断的项目约定——「本项目约定空串不算回文」。它与通行理解相反，任何模型都猜不到，只能靠失败信号发现。改完后脚本后端和真实模型走同一条链路。

**这条对 M2 的影响比对 M1 大**：M2 要建的固定任务集，每个任务都必须先问一句「这个失败真实模型会不会自己避开」。用脚本后端设计出来的任务集，放到真实模型上可能整体退化成「全部一次通过」。

**（b）真实模型的失败路径比脚本多样**

跨多次运行观察到的中断信号分布：

| 信号 | 触发原因 |
|---|---|
| `TEST_FAILED` | 命中隐藏的项目约定——场景本来要验的那条 |
| `SCOPE_VIOLATION` | Subagent 想用 `ls` 探查工作区，撞上 `allowed_binaries=("python",)` |
| `TOOL_FAILURE` | 模型调用本身失败 |

脚本后端只会产生一种。**这意味着脚本后端能验证链路的存在性，但验证不了链路的覆盖面。**

`SCOPE_VIOLATION` 那条暴露了一个工具面设计问题：Runtime 提供了 `read_file` 却没有「列目录」，真实 agent 想探查工作区时只能去调 `ls`，然后被拦。架构师的处理是对的（补一条验收标准，并告诉 Subagent 用 `python -c "import os; print(os.listdir('.'))"` 替代），但这是在用规格补工具面的缺失。留给 M4 考虑是否加 `list_files`。

**（c）中断次数与 token 的关系**

| 中断次数 | token |
|---|---|
| 0 | 4.2k – 5.4k |
| 1 | 7.2k – 8.6k |
| 2 | 17.8k |
| 3 | 23.2k |

每次中断周期（架构师决策 + REBASE 摘要 + 重跑）大致 3–6k token。这是 M2 调 `complexity_threshold` 时的成本基线：**升级给人省下的是这 3–6k，代价是人的注意力**。

**（d）同一场景的运行间方差很大**

同一个 spec、同一个模型，跨 8 次运行的中断次数落在 0–3 之间，token 从 4.2k 到 23.2k。**单次运行不能作为任何参数的依据**。M2 的每个任务至少跑 5 次取分布，否则测出来的阈值是噪声。

**（e）架构师的裁决质量可用**

`deepseek-reasoner` 在 `SCOPE_VIOLATION` 上的判断：「任务目标本身未变，不需要重新分配；但需要在 TaskSpec 中显式补充工具使用约束」——正确区分了「实现问题」和「规格问题」，选了 `MODIFY_TASK` 而非 `REASSIGN`，并给出了可执行的替代方案。这是 §7.1 假设成立的第一个正面证据，但样本量太小，不足以定 `complexity_threshold`。

**（f）多供应商共存时的配置 bug**

`--backend kimi` 曾把 DeepSeek 的 key 发到 LiteLLM 代理上。两个原因叠加：base_url 的预设查表漏了 `kimi` 这个键，key 又走的是后端内部的固定顺序回退链（`DEEPSEEK_API_KEY` 排在前面）。

**只设一家 key 时这个 bug 不会显形**——这类「配置回退链」的错误只在多供应商共存时暴露，值得作为一类固定的回归测试对象。已改成 CLI 侧显式解析每家的 endpoint 与 key。

---

### 11.6 参数实测（M2）

**方法**：15 个任务 × 5 次 = 75 次运行，DeepSeek（架构师 `deepseek-reasoner`，Subagent 与分诊 `deepseek-chat`），SQLite + 本地沙箱，`AutoApproveGate`。累计 1.62M token、8236s 机时（6 并发实际约 25 分钟）。工具在 `src/cowork/bench/`，原始记录 `bench_runs.jsonl`，复现：

```bash
python -m cowork.cli bench --backend deepseek --repeat 5
python -m cowork.cli bench-report bench_runs.jsonl
```

任务集四类形态，每类的隐藏项都按 §11.5a 的判据挑过——全部是**与通行理解相反的项目约定**（保留最后一次出现、不足一块就丢弃、`n<=0` 返回原串…），推理再强也推不出来：

| 类别 | 任务数 | 设计意图 | 实测中断次数（5 次运行） |
|---|---|---|---|
| `PASS` | 3 | 规格完整的对照组 | 0,0,0,0,0 ~ 0,0,0,0,1 |
| `ONE_REBASE` | 4 | 一条隐藏约定 | 多数 1，尾部到 3–5 |
| `MULTI_REBASE` | 3 | 两条独立隐藏约定，逐条暴露 | 2–5 |
| `ESCALATE` | 5 | 架构师不该自己拍板 | 1–5，完成 1/25 |

验收脚本的用例表存成压缩 blob：`read_file` 不受 scope 限制，明文写用例等于把答案发给 Subagent。失败时的报错仍然逐例可读——那是整条链路依赖的证据。

**（a）实测前必须先修的噪声源：探测性读取被当成硬信号**

第一批试跑里三次运行的首条信号全是 `TOOL_FAILURE`。追下去是同一个动作：Subagent 的第一步几乎总是 `read_file("solution.py")` 探一下文件在不在，而「不存在」返回 `ok=False`，`loop.py` 把任何 `not result.ok` 变成 `TOOL_FAILURE` 抢占。

于是**每个任务开局白烧一轮架构师决策**。修正：`ToolResult` 增加 `hard_failure`，探测性查询返回否定答案时置 `False`——结果照样进 `reasoning_trace` 回给模型，只是不产生硬信号。修完 `PASS` 类任务从「2 次中断 / 12.2k token / 78s」降到「0 次中断 / 2.5k token / 10s」。

这条是 §3.2 的一个补充原则：**L0 硬信号是「任务级失败」，不是「任何返回非零的调用」**。

**（b）跑批暴露的崩溃：schema 通过 ≠ 语义有效**

75 次运行里 3 次死于 `ValueError` 穿透（`未知工具: ''`、`'' is not a valid SignalType`）。`ACTION_SCHEMA` 用空串表示「本字段不适用」，所以 `kind=tool_call` + `tool=""` 是合法 JSON、能过本地校验，再往下才炸。

这与 §11.3c 是同一条原则没贯彻到底：模型调用失败已经变成信号了，**模型输出解析失败却还在抛异常**。修正：`_parse_action` 改抛 `ModelCallFailed`，走同一条硬信号通道；同时禁止软信号通道声明硬信号类型（那是 Runtime 的专属职责，§3.1）。

**（c）六个参数的结论**

| 参数 | 原值 | 实测后 | 依据 |
|---|---|---|---|
| `complexity_threshold` | 0.6 | **0.4** | ROC 最佳 Youden 点（TPR 0.66 / FPR 0.35）；原值只有 TPR 0.38。但 AUC 仅 **0.672**，正负类分布大幅重叠 |
| `max_interrupts` | 3 | **3（保留）** | 条件成功率：≥1 次 46%、≥2 次 32%、≥3 次 18%、≥4 次 7%、≥5 次 0%。3 正好在跌破 20% 处 |
| `max_rebase` | 3 | **2** | 完成率 REBASE 2 次 41%、3 次 33%、4 次 0%。第 4 次 REBASE 没救回过任何一次运行 |
| `budget_escalation_ratio` | 0.8 | **0.6** | 0.8 越线后中位只剩 0 token 就到终局，等于事后通知；0.6 提前约 4.8k token，误升级只多 1 次 |
| `step_soft_deadline_s` | 60 | **30** | step 耗时 p50 1.65s / p95 3.11s / p99 5.90s / max 10.79s（n=651）。30s ≈ max 的 3 倍余量 |
| `soft_queue_threshold` / `soft_interval_s` | 5 / 30s | **测不出来** | 见下 |

**最重要的结论不是这些数字，是三件比数字更硬的事**：

1. **`complexity_score` 判别力很弱（AUC 0.672）**。90 条「该升级」的决策里，**63 条是被 §7.2 的确定性规则拦下的，不是被这个分数**。§7.1「LLM 自评复杂度」这条路径的实际贡献远小于设计时的预期，而 §7.2 那句「模型给低分的场合恰恰可能是它没意识到问题严重性的场合」被数据支持了——`e1_silent_failure`（验收脚本静默失败、架构师手上零证据）5 次运行的自评分数中位只有 0.3。

2. **`soft_queue_threshold` / `soft_interval_s` 在当前调用路径上是死参数**。`Architect.should_consume_soft()` 没有任何调用方——orchestrator 在每个检查点无条件批量消费。且软信号极稀疏：75 次运行里 13 次出现过、共 20 条、队列深度最大 2，阈值 5 永远达不到；分诊总成本 13 次调用 × 中位 309 token，占总量 0.3%。**结论是「接上或删掉」，不是编一个数**。

3. **风险 #1 的前提被证伪**。checkpoint 写入耗时中位 **0.2ms**，占 step 总耗时的 **0.009%**。「step 粒度受 checkpoint 开销制约」这个假设在 SQLite + 本地沙箱下不成立，step 粒度可以完全按中断响应延迟来定。另外 `step_soft_deadline_s` **当前没有任何代码读它**，`loop.py` 并未实现 soft deadline 切段。

**（d）中断的成本结构：架构师占掉一半 token**

| 中断次数 | token 中位 | 样本 |
|---|---|---|
| 0 | 3.1k | 16 |
| 1 | 10.5k | 18 |
| 2 | 16.8k | 8 |
| 3 | 27.1k | 6 |

每次中断周期的边际成本约 **7k token**（M1.3 按小样本估的是 3–6k，偏低）。拆开看：`decide_interrupt` 176 次调用、中位 3536 token；`summarize` 86 次、中位 214；**架构师（决策 + 摘要）占总消耗的 51.3%**。

这个数字改变了 §7.1 的成本论证方向：升级给人省下的不是「3–6k」而是「7k + 后续可能的连锁中断」，但代价是——**架构师本身就是系统里最贵的组件，它的每次介入都在花掉一半以上的预算**。

**（e）隐藏约定其实是被 Subagent 自己发现的，不是被 REBASE 修好的**

追一次 `r1_palindrome_empty` 的完整轨迹：Subagent 写完实现后**自己跑了一遍 `python verify.py`**，看到 `FAIL: is_palindrome(*['']) -> True, expected False`，下一步就改对了。整个过程里架构师被叫来一次，说的是「这是个小修，CONTINUE」。

两个推论：

- **`run` 返回非零即抢占，把 Subagent 正常的自测-修复循环切成了「每失败一次就叫一次架构师」**。这是 §3.2 的明文设计（`TOOL_FAILURE` 是 L0），但实测显示它的代价具体是多少：`TOOL_FAILURE` 占全部硬信号的 50%（89/178）。是否该把「Subagent 主动执行验收命令」与「工具坏了」区分开，留给 M4 定。
- **验收命令一旦对 Subagent 可执行，「隐藏要求」就不再隐藏**——失败信息本身把答案说出来了。这是 §11.5a 那条教训的新形态：不是模型能推断出隐藏要求，而是**模型能直接读到验收失败的详细信息**。任务集因此仍有区分度（中断确实发生了），但它验证的是「失败信号驱动收敛」，不是「架构师改规格驱动收敛」。

**（f）`ls` 问题的量化**

§11.5b 记过一次：Runtime 有 `read_file` 却没有「列目录」，真实 agent 想探查工作区只能去调 `ls`，然后撞 `allowed_binaries`。M2 给出了频率：**75 次运行触发 23 次 `SCOPE_VIOLATION`，分布在 15 个任务里的 10 个**，包括本该零中断的 `PASS` 类。

这不是边缘情况，是约三成运行都会走的路径。追踪确认原因就是 `ls`。**建议把 `list_files` 从 M4 提前**——它同时消掉一个 30% 命中率的假阳性升级源。

**（g）方法论局限（结论的适用边界）**

- **标注是任务集作者做的**，不是独立盲标。`should_escalate` 的口径写在 `tasks.py` 每个任务的 `hidden` 里，可复核但不独立。ROC 的绝对值应看作乐观估计。
- **标签是任务级的**，一个任务的所有中断共用一个标签。实际上 `ONE_REBASE` 任务在第 4 次中断时也该找人了，这会压低正类分数、进一步拉低 AUC。
- **单一供应商、单一沙箱模式**。checkpoint 开销结论只对 SQLite 成立，Postgres 需重测；step 耗时只对 `deepseek-chat` 成立。
- **`e3_scope_bait` 没按设计触发**。它想诱导模型去写 scope 外的 `helper.py`，实测模型多数直接放弃，`SCOPE_VIOLATION` 反而是从别的任务的 `ls` 来的。这个任务需要重新设计。
- **`AutoApproveGate` 意味着「升级」不产生真实的人类判断**，只是记录了升级发生。升级决策的**质量**没有被验证，那是 M5 的事。

---

### 11.7 PROBE 实测（M3）

**实现落点**：PROBE 要调模型，所以判定逻辑**不能进 `runtime/`**。Runtime 只做一个确定性判断——「距上次探查是否已到间隔」，到点就在 step 边界返回 `probe_due=True` 让出控制权；看不看得懂产出是架构师的事。这条切分就是不变量 2（Runtime 不含 LLM）在 PROBE 上的落地。

三个衍生结论：

- **探查不是中断**。在轨就直接接着跑，不消耗 cycle、不换 Subagent、不动 revision。只有判定跑偏才升级成信号。
- **跑偏信号由 Orchestrator 发，不是 Runtime 发**，类型复用 `VALIDATION_FAILED`、用 `payload.origin="architect_probe"` 区分。判定来自模型就不能叫「Runtime 确定性产生」，但走的是和「架构师验收不通过」完全相同的既有路径（§5 流程图右下角），不新开决策通道。
- **产出内容由 sandbox 读出来再传给架构师**。架构师没有也不该有文件系统访问权，读文件是确定性操作。

**方法**：同一个写作任务（分四个文件，两条都不可机器检查的验收标准）三个 arm，只差 `silence_policy` 与探查间隔，各跑 5 次。数据在 `probe_runs.jsonl`。

| arm | 策略 | 探查次数(中位) | token 中位 | 表面溢价 |
|---|---|---|---|---|
| `g0_trust` | TRUST | 0 | 8024 | 1.0x |
| `g1_probe_20s` | PROBE 20s | 1 | 27259 | **3.40x** |
| `g2_probe_5s` | PROBE 5s | 4 | 29080 | **3.62x** |

**（a）3.4x 这个数字是错的，不能拿它触发 §12 M3 的判断点**

单次探查中位只有 **1176 token**（n=27）。1 次探查换来 +19k token 的差额，算术上就说不通。拆开 token 归因后原因很清楚：**三个 arm 抽到的中断次数不同**（中位 1 / 3 / 4），而 M2 已经量过每次中断约 7k token。差额几乎全部来自 `decide_interrupt`，不是来自 `probe`。

把中断次数控制住再比——只看零中断的运行：

| arm | 零中断样本 | token | 探查占比 |
|---|---|---|---|
| `g0_trust` | 6772 / 8024 | 中位 ~7.4k | — |
| `g1_probe_20s` | 9751 / 11642 | 中位 ~10.7k | 12.7% |
| `g2_probe_5s` | 12160 | 12.2k | 15.8% |

**PROBE 的净溢价是 1.45x（20s）/ 1.64x（5s），不是 3.4x**。§12 M3 那条「溢价 >3x 就回头重新考虑 §3.2.1」的判断点**不该被触发**——它差点被一个没控制变量的中位数比较误触发。这是 §11.5d「单次运行是噪声」的推广形态：**跨 arm 比中位数时，如果 arm 之间的高方差项没被控制住，中位数比较本身就是噪声**。

**（b）比成本更值得记的：PROBE 一次都没抓到东西**

27 次探查，**0 次判跑偏**。花掉 12–16% 的 token，收益在这批数据上是零。两种可能，都还没被区分开：任务本身没漂移（模型确实全程守住了第二人称），或者探查提示词太宽松（`PROBE_SYSTEM` 明确写了「拿不准就判在轨」，因为误报的代价是白打断一次）。

**这意味着 PROBE 的成本已知、收益未知**。要标定收益，需要一个**会可靠漂移**的 GENERATIVE 任务——而设计这种任务的难度和 §11.5a 那条教训同源：模型不会按你的期待失败。`default_probe_interval_s` 因此只有成本侧的依据（20s → 1–2 次/运行），收益侧无约束，保守取长不取短。

**（c）GENERATIVE 的中断链路确实跑通了，但走的是 verify 而不是 probe**

三个 arm 共出现 21 条 `VALIDATION_FAILED`，全部来自架构师的**终局验收**（`origin=architect_verify`）——第二人称那条约束真实地被违反过。M3 的出口「一个 GENERATIVE 任务能跑通中断链路」达成，只是达成路径与预期不同：**对这个任务，终局验收比中途探查更有效**。

**（d）实现期踩的坑：探查分段会把资源上限清零**

第一版实现里，每次探查后重新进 `loop.run`，`max_steps` 和 `deadline_s` 的计数跟着清零——PROBE 任务因此变成**没有步数上限也没有超时**。pilot 里 `max_steps=10` 的任务跑了 13–14 步才暴露。

这个 bug 的严重性在于它精确地打掉了唯一还剩的护栏：§3.2.1 说 GENERATIVE 只剩 `TIMEOUT` / `STEP_LIMIT` / `BUDGET_EXCEEDED` 三条硬信号，而它一次干掉两条。修正是把已用预算跨分段传递（`cycle_steps_used` / `cycle_started`）。**给循环加"让出控制权"的能力时，所有以循环为计量单位的东西都要跟着走**。

另有一条必需的护栏：探查在轨后 Orchestrator 会重置计时并再次进入，若此时间隔已过而一个 step 都没派发，就会「探查 → 无进展 → 再探查」空转烧 token。所以 `probe_due` 要求本段至少派发过一个 step。

---

### 11.8 并行与冲突检测（M4）

**出口达成**：4 个子任务、2 种 `task_class`、最大并行度 2 的复合任务在 DeepSeek 上跑通，全部 `COMPLETED`，25.9s / 25.7k token / 零中断（`demo_composite.py`，`python -m cowork.cli composite --backend deepseek`）。

**（a）并行度加在调度层，不是通信层**

`Scheduler` 按 `depends_on` 拓扑分层，层内用线程池并行跑多个 `Orchestrator`。**没有新增任何「任务 ↔ 任务」的 API 面**——下游拿到上游成果的唯一途径是调度器把 artifact 作为只读上下文注入（§8 传引用不传全文）。§1.4 第一条约束（无中心并行错误放大 17.2x）因此仍然成立：并行的是执行，中心化的是决策。

**（b）可分解性是算出来的，不是模型说了算**

`plan.py` 全部确定性、无 LLM：拓扑分层（有环直接抛，不靠运行时兜底）、scope 交集检测、可分解性评估。两条判定规则：

- **同层 scope 有交集 → 整层串行化**。不做「求最大独立集」的部分并行优化：收益不确定，而错了的代价是**静默覆盖**——并行写同一个文件不会报错，先写的那份产出就那么没了。
- **没有任何一层能并行 → 标记 `fan_out` 问题**。§1.4 第三条：顺序依赖强的任务多 agent 相对单 agent 最差 −70%，这种拆解应该退化为单 agent。

**（c）冲突检测：新增一条 L0 硬信号**

`CONFLICT_DETECTED` 是 §3.2 硬信号清单的第 9 条，与 L1 的 `CONFLICT_SUSPECTED` 是两回事：后者是 Subagent「怀疑」，前者是调度器**确定性观测到**两个任务写了同一份产出。§3.1 已确立软信号靠不住，而 4.3 要的正是「从被动转主动」。它对所有 `task_class` 都可用（并入 `_ALWAYS`）——冲突在产出层检出，与任务本身有没有内容层判据无关，**GENERATIVE 正因为没有判据才更需要这条兜底**。

**「同层」这个限定是本质的，不是优化**：跨层写同一个文件是**有序的交接**（下游在上游产出上继续做），那是拆解的正常形态。只有并行写才会「谁后写谁赢」。把跨层也判成冲突会让正常拆解跑不动。

于是运行期还能撞上的冲突只剩一种：**架构师在运行中用 `MODIFY_TASK` 改宽了 scope**，把两个并行任务撞到一起——静态检查看不到，因为它检查的是派发前的声明。这条路径有测试固定（`test_scheduler.TestConflictDetection`）。

**（d）仲裁不新开决策通道**

冲突被表达成一条硬信号、归属给**后写的那个任务**（按 artifact `created_at`，确定性），然后走既有的 `Architect.decide()`——它本来就会 `MODIFY_TASK`（改窄 scope）/ `REASSIGN` / `ABANDON`。为冲突单开一套裁决逻辑，等于承认「架构师是唯一写入决策点」（§2.3）不成立。

**（e）并行暴露的存储层缺陷**

`sqlite3` 连接不是线程安全的，而并行调度让多个 `Orchestrator` 同时写同一个 store。已改为 `check_same_thread=False` + 方法级 `RLock`。锁必须覆盖到 `fetch`——只锁 `execute` 的话游标会在锁外回头碰连接。写入量很小，串行化的代价可以接受；静默的数据竞争不可以。

---

### 11.9 架构师的停止判断（M5a）

M2 归因（§12 M5）定的方向：架构师的失效形态不是「规格拆错了」，是**「不知道该停」**。三条改动：

| # | 改动 | 落点 | 成本 |
|---|---|---|---|
| a | 决策无效的确定性判据 | `escalation.py` + `signals.fingerprint()` | 零 token |
| b | 把决策历史喂给架构师 | `Backend.decide_interrupt(history=...)` | 每次多几百 token |
| c | 给 `ABANDON` 写明判据 | `ARCHITECT_SYSTEM` | 零 |

**（a）把「架构师无效」变成可确定性观测的事实**

`signals.fingerprint(signals)` = 信号类型 + 证据内容的哈希。连续两次中断指纹相同，说明架构师上一次的决策**做了动作但世界没变**。这条比 `max_interrupts` 更早也更准：它区分「试了三次不同的办法」和「同一个办法试了三次」。

指纹对证据取哈希而非留原文——它要进日志和 `DecisionRecord`，而 `raw_evidence` 可能很长、也可能含第三方错误体。

**（b）架构师此前看不到自己的裁决**

`decide_interrupt` 的输入只有 `spec + signals + produced`，既没有前几轮的 `DecisionRecord`，也没有 `interrupt_count`。它**每次都在「第一次见到这个问题」的状态下决策**——M2 实测里 `e1_silent_failure` 那串 `CONTINUE → CONTINUE → CONTINUE` 就是这么来的。这份历史由架构师自己维护（`Architect._history`），同时喂确定性判据和提示词。

**（c）第一版是一次真实的回归，而且只有对照组能发现**

第一版把 `ABANDON` 的判据写成三条并列条件，并强调「这不是最后手段」。在不可解任务上效果惊人：主动 `ABANDON` 从 12% 涨到 96%，token 中位从 39.4k 降到 15.3k。

**如果只看这一组数据，我会把它当成改进提交。**

可解任务的对照组说的是另一回事：

| 版本 | 可解任务完成率 | 误放弃 | `MULTI_REBASE` |
|---|---|---|---|
| v0 基线 | 39/48 = **81%** | 0 | 5/13 |
| v1 第一版 | 28/50 = **56%** | 22 | **0/15** |

动作分布更刺眼：v1 的 50 次运行里，架构师做出的决策**只有一种——`ABANDON` 22 条，`MODIFY_TASK` 归零**。所谓的 12%→96% 不是判别力变强，是**它现在无差别地放弃**。偏置被推到了另一端。

第二版按「先判断证据的性质，再选动作」重写：证据具体且指向规格缺口 → `MODIFY_TASK`（并明写「这是最常见的情形，也是这个系统存在的意义」）；只有「继续下去不可能成功」**且**「改 TaskSpec 也解决不了」才 `ABANDON`。

三版的判别力（把「该不该放弃」当二分类，n≈25 / n≈50）：

| 版本 | TPR（该弃则弃） | FPR（误弃） | Youden J |
|---|---|---|---|
| v0 基线 | 0.12 | 0.00 | 0.12 |
| v1 第一版 | 0.96 | 0.44 | 0.52 |
| **v2 现版** | **0.80** | **0.20** | **0.60** |

v2 在可解任务上完成率 80%（基线 81%，`MULTI_REBASE` 反而从 5/13 升到 7/15），在不可解任务上中断中位从 5 降到 2、token 中位从 38.5k 降到 33.7k。**这是判别力的真实提升，不是偏置平移**——v1 也提升了 J，但 v2 用一半的误放弃拿到了更高的 J。

**（d）确定性判据是护栏，不是主力**

停滞判据的命中次数：v0 **0** → v1 **1** → v2 **17**。

v1 里它几乎不触发，因为运行在第一次中断就结束了，没有「重复」可言。**只有当提示词不再无差别放弃，这条护栏才有事可做**。三条改动是互补的：(c) 提供判别，(a) 兜住判别失效的情形，(b) 让 (c) 的「同样的信号又原样出现了」这句话有依据可查。

**（e）误放弃的代价被一条结构性规则兜住了**

v2 仍有 20% 的可解任务被误放弃。为此新增 `policy.escalate_on_abandon`：**任何 `ABANDON` 都升级给人**——放弃对该任务是不可逆的，按 §7.2 第 1 条同理该由人拍板。原先只有顶层任务的 `ABANDON` 会升级。

这条把误放弃的后果从「任务没了」降级成「打扰人一次」。**它在实测数据里看不出效果**：`AutoApproveGate` 会直接采纳 LLM 裁决，所以 bench 的数字一个都不会变。它的依据是结构性的，不是实测的——这一点必须写明，否则就是拿一条未验证的改动混在已验证的结论里。

---

### 11.10 拆解复核（M5b）

**先说一个必须澄清的前提：架构师从来没有真的拆解过任务。**

`Orchestrator` 拿到的是现成的 `TaskSpec`，`Scheduler` 拿到的是现成的列表，`demo_composite.py` 那 4 个子任务是手写的。§2.3 把「任务拆解与可分解性评估」列为架构师职责，**但生成侧从未实现**——这是 §11.1 文档↔代码对照表里此前漏记的一项。

所以 M5b 做的是**复核侧**：它对任何一组子任务都成立，不依赖生成侧存在。

**两层，可信度不同，所以在数据结构上也是分开的两个字段：**

| 层 | 查什么 | 成本 | 会不会漏判自己 |
|---|---|---|---|
| `plan.deterministic_review()` | 结构：依赖悬空、有环、无 scope、无验收标准、拆了等于没拆 | 零 | 不会 |
| `Backend.review_decomposition()` | 语义：满足这些验收标准是不是就等于完成原始目标 | 一次调用 | 会 |

顺序是先结构后语义：**结构就是坏的时候，语义复核既没有意义也不该为它花 token**。

**方法选的是「验收标准反推」而不是「拆解后独立复核」**（§12 M5 候选方案表里的第二条）。反推的方向是刻意的：正向问「这个拆解好不好」得到的是复述，反推问「按这些标准验收完还缺什么」才逼出遗漏。

**实测**：拿 M4 的复合场景做两组，各 5 次（DeepSeek）：

| 输入 | 期望 | 结果 | 单次 token |
|---|---|---|---|
| 完整的 4 个子任务 | `sufficient=true` | **5/5 正确，零假阳性** | 845–2219 |
| 摘掉 `t2_format` | `sufficient=false` | **5/5 正确**，每次都点名缺的是格式化 | 845–2219 |

模型的原话是「没有任何子任务负责实现格式化组件（如 `formatter.format_row`），原始目标中的『格式化』部分未被覆盖」。**M5 的出口标准「能验证出原本会漏掉的拆解错误」达成。**

顺带一个免费的收获：摘掉并行的那一支之后，剩下三个任务退化成一条链，**结构检查的 `fan_out` 自己就先叫了一声**。它抓不到「缺了格式化」，但零成本地抓到「这个拆解已经没有并行度了」。

**局限（决定了这条结论的适用边界）**：

- **复核者和拆解者是同一个模型**。这只是「同一个脑子换个问法再想一遍」，不是独立复核。真正的独立需要另一个供应商或人。风险 #3 的核心——「架构师是唯一没被验证的环节」——因此只被削弱，没有被消除。
- **只测了一种缺陷形态**（整个子任务缺失）。更隐蔽的形态（验收标准写了但判据太松、子任务之间的衔接没人验收）没有被验证。
- **生成侧不存在**，所以「架构师自己拆出来的东西质量如何」仍然是空白。

---

## 12. 开发路线图

### 总览

```
M0 ✅ 核心链路验证        ← 已完成
M1 ✅ 真实环境收口        ← 已完成（4/4）
M2 ✅ 参数实测            ← 已完成（§11.6，75 次真实运行）
M3 ✅ PROBE 模式          ← 已完成（§11.7，成本已知 / 收益未知）
M4 ✅ 并行与冲突检测      ← 已完成（§11.8，4 子任务复合场景跑通）
M5a ✅ 停止判断           ← 已完成（§11.9，判别力 J 0.12 -> 0.60）
M5b ✅ 拆解复核（复核侧）  ← 已完成（§11.10）。**生成侧仍未实现**
M7    拆解三角色          ← 下一步。生成者 / 复核者 / 人，关掉风险 #3 + #14
M6    群聊界面层          （与 M7 无耦合，可并行或后做）
```

**M7 排在 M6 前面**：它关掉两条风险（架构师不会拆解、复核者不独立），M6 只做界面、验证不了任何架构假设。两者无代码耦合，可交给不同的人并行做。

**排序理由**：M2 必须在 M1 之后——用 `scripted` 后端测出的 `complexity_threshold` 和 step 耗时是自证的假数据。M5 需要 M2 的实测数据支撑，因为「架构师拆解质量」只有在参数稳定后才可归因。M6 唯一与产品价值直接相关，但验证不了任何架构假设，可交给独立的人并行做。

---

### M1 — 真实环境收口

**目标**：把 §11.4 原列的三项「已写未测」变成「已验证」。

| # | 任务 | 出口标准 | 状态 |
|---|---|---|---|
| 1.1 | 启动 Postgres，跑通 `test_postgres_store` | 3 个 skip 变 pass；两条 CHECK 约束验证生效（故意写扁平 context 应被拒） | ✅ |
| 1.2 | Docker 沙箱实测 | 越权访问真实触发 `SCOPE_VIOLATION`，而非仅工具层白名单拦截 | ✅ 实测后改了实现，见 §10.4.1 |
| 1.3 | 接 LiteLLM + 真实模型 | demo 场景用真实模型跑通 | ✅ DeepSeek / Kimi，见 §11.5 |
| 1.4 | virtual key 落地 `token_budget` | 超预算时 LiteLLM 侧真实拒绝，验证 §7.2 成本兜底不只是应用层软限制 | ✅ 见 §10.3.1 |

**1.3 的口径修正**：原出口写的是「架构师用 opus，分诊用 haiku」。实际无 Anthropic key，改为供应商无关——只要求 demo 场景在真实模型下跑通，具体分工按供应商能力定（DeepSeek 方案：架构师 `deepseek-reasoner`，Subagent / 分诊 `deepseek-chat`）。为此新增了 OpenAI 兼容后端，理由见 §10.3.2。

**里程碑出口**：✅ demo 场景在「真实模型 + Postgres + Docker 沙箱」下完整跑通。
测试全绿，零 skip（四类真实服务全部在线）。

**风险回顾**：1.4 的返工风险**确认存在**，且比预想的隐蔽——不是「映射不了」，而是**预算拒绝与真实限流同为 HTTP 429**，按状态码判断会把限流误判成预算耗尽。转换层已建，位置从 `detectors.py` 改到 `llm/errors.py`（理由见 §10.3.1）。

**M1 阶段新增的未预期工作**：
- OpenAI 兼容后端（§10.3.2）——原路线图假设只对接 Anthropic
- 模型调用失败的信号化（§11.3c）——原实现会让整个 run 崩

---

### M2 — 参数实测

**目标**：把 `policy.py` 里六个猜测值变成有依据的结论。

**前置**：需要先建一个 **10–20 个任务的固定任务集**（覆盖：一次通过 / 需一次 REBASE / 需多次 REBASE / 应升级给人）。没有任务集，所有参数都只能靠感觉。

M1.3 实测给这个前置加了两条硬要求（§11.5a / §11.5d）：

1. **每个任务都要先验证真实模型不会自己避开该失败**。用脚本后端设计的失败场景，放到真实模型上可能整体退化成「全部一次通过」——demo 场景就踩过这个坑。判据：这个「隐藏要求」是客观缺失，还是只是没写全但可合理补全？后者不算规格不清。
2. **每个任务至少跑 5 次取分布，不能用单次结果**。同一 spec 同一模型跨 8 次运行，中断次数落在 0–3 之间，token 从 4.2k 到 23.2k。单次运行测出来的阈值是噪声。

这两条会显著抬高 M2 的实测成本：20 个任务 × 5 次 ≈ 100 次运行。按 §11.5c 的 token 基线（4–23k/次）估，量级在 1M token 左右，DeepSeek 价位下成本可接受，但**时间成本**要提前算进去。

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

#### M2 结果（已完成）

**出口达成**：`policy.py` 六个参数每个都带一行实测依据。完整数据与方法见 **§11.6**。

| # | 任务 | 出口标准 | 状态 |
|---|---|---|---|
| 2.1 | 建 10–20 个任务的固定任务集 | 四类形态齐备，且每个任务先验证真实模型不会自己避开该失败 | ✅ 15 个任务，`bench/tasks.py` |
| 2.2 | 每任务 ≥5 次跑批 + 仪表化 | 记录到 step 耗时、checkpoint 开销、token 轨迹、每条决策的自评分数 | ✅ 75 次运行，1.62M token |
| 2.3 | 六个参数各有实测依据 | 写进 `policy.py` 注释 | ✅ 三个下调、一个保留、两个「测不出来」 |

**改了四个值**：`complexity_threshold` 0.6→0.4、`max_rebase` 3→2、`budget_escalation_ratio` 0.8→0.6、`step_soft_deadline_s` 60→30。`max_interrupts=3` 是唯一被数据支持保留原值的。

**比参数更重要的三条结论**（§11.6c）：LLM 自评复杂度判别力弱（AUC 0.672），真正在拦风险的是 §7.2 的确定性规则；`soft_queue_threshold` / `soft_interval_s` 在当前调用路径上是死参数；风险 #1 的前提（checkpoint 开销制约 step 粒度）被证伪。

**M2 期间修的两个缺陷**：探测性 `read_file` 被当成硬信号（§11.6a）、动作解析失败抛异常穿透整个 run（§11.6b）。前者修完 `PASS` 类任务的 token 消耗降到原来的 1/5。

**M2 给 M4 加的两项**（都来自实测频率，不是猜测）：

- **`list_files` 工具**（原列在 M4 的「考虑」，现在是必做）。缺它导致 30% 的运行触发假阳性 `SCOPE_VIOLATION`（§11.6f）。
- **区分「Subagent 主动跑验收命令失败」与「工具坏了」**。当前 `run` 非零即抢占，占全部硬信号的 50%，把正常的自测-修复循环切成反复打断架构师（§11.6e）。

**M5 因 M2 更紧迫**：架构师占总 token 的 51.3%，是系统里最贵、最有权、且唯一无人复核的组件。

---

### M3 — PROBE 模式（`GENERATIVE` 类）

**目标**：解掉 `Orchestrator.__init__` 里对 `silence_policy=PROBE` 抛的 `NotImplementedError`，让没有客观判据的任务可被观测。

| # | 任务 | 说明 |
|---|---|---|
| 3.1 | 实现按 `probe_interval` 主动索要中间产出 | 架构师发起，不等 Subagent 上报 |
| 3.2 | 中间产出的验收逻辑 | 复用 `acceptance` 中 `machine_checkable=false` 的标准，交模型判断 |
| 3.3 | 实测 PROBE 的 token 成本 | 与 `TRUST` 模式对比，量化「观测能力缺失的代价」 |
| 3.4 | 定 `probe_interval` 默认值 | 成本 vs 跑偏发现延迟 |

**里程碑出口**：一个 `GENERATIVE` 任务能跑通中断链路，且 PROBE 的成本溢价有明确数字。

**判断点**：如果 3.3 测出成本溢价过高（比如 >3x），需要回头重新考虑 §3.2.1 的设计——可能要引入「产出增量的确定性检查」（字数、结构完整性）作为廉价的伪硬信号，而不是全靠模型验收。

---

#### M3 结果（已完成）

**出口达成**，完整数据见 **§11.7**。

| # | 任务 | 状态 |
|---|---|---|
| 3.1 | 按 `probe_interval` 主动索要中间产出 | ✅ `loop.py` 只判「到点了」，架构师判「在不在轨」 |
| 3.2 | 中间产出的验收逻辑 | ✅ `Backend.probe()`，与 `verify` 分开——一个问「方向对吗」，一个问「完成了吗」 |
| 3.3 | 实测 PROBE 的 token 成本 | ✅ 净溢价 **1.45x（20s）/ 1.64x（5s）** |
| 3.4 | 定 `probe_interval` 默认值 | ⚠️ `default_probe_interval_s=20.0`，**只有成本侧依据** |

**判断点没有触发，但差点被误触发**：表面中位数溢价是 3.40x / 3.62x，超过 3x 阈值。拆开归因后发现差额来自三个 arm 抽到的中断次数不同（中位 1/3/4 × 每次约 7k token），不是来自探查——单次探查中位只有 1176 token。控制住中断次数后净溢价是 1.45x / 1.64x。**所以不需要回头改 §3.2.1**。

**真正该记的是另一件事**：27 次探查 **0 次判跑偏**。PROBE 的成本已经量出来了，**收益还没有**。要标定收益需要一个会可靠漂移的 `GENERATIVE` 任务，而设计这种任务的难度与 §11.5a 同源。`probe_interval` 的默认值因此只有成本侧约束，保守取长不取短。

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

#### M4 结果（已完成）

**出口达成**：4 个子任务、2 种 `task_class`、并行度 2 的复合任务在 DeepSeek 上跑通，全部 `COMPLETED`（25.9s / 25.7k token / 零中断）。完整分析见 **§11.8**。

| # | 任务 | 状态 |
|---|---|---|
| 4.1 | 多 Subagent 并行调度 | ✅ `scheduler.py`，层内线程池。**没有新增任务间 API 面** |
| 4.2 | `depends_on` 拓扑排序与可分解性评估 | ✅ `plan.py`，全确定性无 LLM；无并行度时标记 `fan_out` |
| 4.3 | 冲突检测从被动转主动 | ✅ 新增 L0 信号 `CONFLICT_DETECTED`，产出层确定性检查 |
| 4.4 | 冲突的合并策略 | ✅ 归属后写方 → 走既有 `Architect.decide()`，不新开决策通道 |

**M2 交办的两项也在本阶段落地**：`list_files` 工具（消掉三成运行的假阳性 `SCOPE_VIOLATION`）、区分「Subagent 预演验收命令失败」与「工具坏了」（原先前者占全部硬信号的 50%）。

**两个设计取舍值得记**：

- **「同层」是冲突的必要条件**。跨层写同一个文件是有序交接，判成冲突会让正常拆解跑不动。于是运行期能撞上的冲突只剩「架构师用 `MODIFY_TASK` 改宽了 scope」这一种——静态检查恰好看不到的那种。
- **同层 scope 有交集就整层串行**，不做部分并行优化。收益不确定，而错了的代价是静默覆盖。

**并行暴露的存储层缺陷**：`sqlite3` 连接不是线程安全的，已改为 `check_same_thread=False` + 方法级 `RLock`（§11.8e）。

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

#### M2 数据对 M5 的归因（2026-08-10，结论：拆 M5a / M5b，M5b 等 M4）

上面那句「先统计一下」已经用 M2 的 75 次运行做了。结果与 M5 原本的假设**不一致**：

| 观察 | 数字 |
|---|---|
| 架构师改规格改坏过原始意图吗 | **0 / 25**（改过规格且完成的运行，独立意图检查全过） |
| `ESCALATE` 类里架构师主动 `ABANDON` | **3 / 25 = 12%** |
| `ESCALATE` 类被确定性上限兜住 | **20 / 25 = 80%** |
| 176 条决策的动作分布 | `MODIFY_TASK` 92 / `REASSIGN` 51 / `CONTINUE` 30 / **`ABANDON` 3** |
| 自评分数（最终失败 vs 最终成功） | 中位 0.4 vs 0.3，基本重合 |

最刺眼的是 `e1_silent_failure`——验收脚本静默失败，架构师手上**零证据**。五次运行的动作序列是 `CONTINUE / REASSIGN / MODIFY_TASK` 轮流试，**一次 `ABANDON` 都没有**，五次全部 `FAILED`。没有任何依据时它选择继续猜。

**结论：架构师在本批数据里的失效形态不是「规格拆错了」，是「不知道该停」。** 真正承担停止职责的是 `policy.py` 里的计数器，不是它的判断。

**但这批数据测不到「拆解」**——`bench/tasks.py` 里每个任务都是一个现成的 `TaskSpec` 直接进 `Orchestrator`，`parent_id` 是为绕开 §7.2 顶层保护而设的虚拟值，**全程没有架构师做分解这一步**。风险 #3 的原始表述是「规格拆解错误无人纠正」，而拆解要到 M4 的多任务场景才真实存在。

因此 M5 拆成两半：

**M5a — 停止判断**（数据支持，M4 之后即可做）

| 方案 | 思路 | 成本 |
|---|---|---|
| 决策无效的确定性判据 | 连续 N 次中断的**信号类型 + 证据指纹相同** → 决策没改变现实 → 强制升级，不问 LLM。落在 `escalation.py`，与 §7.2 同源 | 零 token |
| 把决策历史喂给架构师 | `decide_interrupt` 当前的输入只有 `spec + signals + produced`，**看不到前几轮的 `DecisionRecord`，也看不到 `interrupt_count`**——它每次都在「第一次见到这个问题」的状态下决策 | 每次多几百 token |
| 给 `ABANDON` 写明判据 | `ARCHITECT_SYSTEM` 里它是「方向错误，放弃」，门槛写得过高，实际使用率 3/176 | 零 |

第一条最符合本设计的地基：它把「架构师无效」变成一个**可确定性观测的事实**——决策做了，但世界没变。

**M5b — 拆解复核**（原 M5 的三个候选方案，**必须等 M4**）

在有真实分解之前，「拆解质量」既不可测也不可复核。M4 的出口标准（一个拆成 3–5 个子任务、含两种 `task_class` 的复合任务）正好是 M5b 的最小验证场景。

---

#### M5 结果（已完成，出口达成）

**里程碑出口「至少一种机制上线，且能验证出原本会漏掉的拆解错误」已达成**：验收标准反推在 M4 复合场景上 10/10 正确、零假阳性（§11.10）。

| # | 任务 | 状态 |
|---|---|---|
| M5a-1 | 决策无效的确定性判据 | ✅ `signals.fingerprint()` + §7.2 新增一条；v2 命中 17 次 |
| M5a-2 | 把决策历史喂给架构师 | ✅ `Backend.decide_interrupt(history=...)` |
| M5a-3 | 给 `ABANDON` 写明判据 | ✅ 但**第一版是回归**，见 §11.9c |
| M5a-4 | `ABANDON` 一律升级给人 | ✅ `policy.escalate_on_abandon`。**实测无法验证**（AutoApproveGate 下看不出效果），依据是结构性的 |
| M5b-1 | 结构性复核（免费） | ✅ `plan.deterministic_review()` |
| M5b-2 | 验收标准反推（一次调用） | ✅ `Backend.review_decomposition()`，10/10 |

**三个候选方案的取舍**：选了「验收标准反推」——它便宜（一次调用、845–2219 token）、方向对（反推逼出遗漏，正向只会得到复述）。「拆解后独立复核」没做，因为在只有一个供应商的配置下它退化成同一个模型再想一遍，与已做的这条没有实质区别；「事后归因」需要长期样本积累，不适合现在做。

**风险 #3 只被削弱，没有被消除**。两条硬约束仍在：复核者与拆解者是同一个模型；**拆解生成侧从未实现**（§9 风险 #14）。

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

### M7 — 拆解的三角色：生成者 / 复核者 / 人

**目标**：补上「架构师不会拆解」这个空白（风险 #14），同时把 M5b 那条局限——复核者与拆解者是同一个模型——真正解掉（风险 #3）。

**与 M6 无代码耦合，建议先于 M6 做**：它关掉两条风险，M6 只做界面。

#### 角色与权限（这一节是这个阶段的地基）

「三个架构师」这个说法要避免——其中两个没有写权，沿用它会让人以为 §2.3 的「唯一写入决策点」被放弃了。准确的表述是：

| 角色 | 权限 | 现状 |
|---|---|---|
| **生成者** | **有写权**，产出 `TaskSpec` | **完全不存在**，这是唯一的真空白 |
| **复核者** | **无写权**，只产出 findings | 协议位已在（`Backend.review_decomposition`），缺「换个模型跑」 |
| **人** | 仲裁 | 已是独立角色（§2.4 / `HumanGate` / §7.2） |

**§2.3 的「唯一写入决策点」不变，写权仍然只在生成者手上。** 复核者是顾问，人是仲裁者。真正会破坏不变量的做法是「复核者可以直接驳回或改写 spec」——那就成了两个写入点。

§1.4 第一条约束（执行层中心化、错误放大 17.2x→4.4x）管的是 **Subagent 之间**，架构师这一层加一个无写权的顾问不触及它。

#### 裁决规则：生成 → 复核 → 重生成 ≤N 次 → 升级给人

三个候选里选第三个：

| 方案 | 问题 |
|---|---|
| 复核意见只是信息，生成者自行取舍 | 等于没做复核——M2 已证明架构师自评判别力弱（AUC 0.672），M5a 又证明提示词只能调偏置 |
| 复核不通过就升级给人 | 最诚实，但每次拆解都占用人的注意力 |
| **重生成 ≤N 次，仍不通过才升级给人** | **选它** |

**选它的关键理由不是折中，是同构**：

```
执行层：  派发 → 验收   → REBASE   → 超 max_rebase → 升级给人
拆解层：  生成 → 复核   → 重生成   → 超上限        → 升级给人
```

所以**不要为拆解层新建一套平行的中断/恢复机制**。`escalation.py` 的确定性下限、`policy.py` 的上限、M5a 那条「指纹重复 = 决策没改变现实」的判据（拆解层的指纹 = 复核 findings 的哈希）全部原样复用。**如果发现自己在写一套平行的逻辑，说明方向错了。**

#### 任务

| # | 任务 | 规模 | 说明 |
|---|---|---|---|
| 7.1 | 复核者换独立模型 | **小** | `Architect(..., reviewer_backend=None)`。基础设施现成：`.env` 已有 DeepSeek + Kimi 两家 key，`openai_compat` 支持任意供应商 |
| 7.2 | 跨模型复核对照实测 | 中 | **先做这个**，见下方顺序 |
| 7.3 | 生成者 | **中** | `Backend.decompose()` + prompt + schema + `TaskSpec` 组装。确定性校验直接复用 `plan.deterministic_review()` |
| 7.4 | 生成-复核循环 + 上限 | 小 | 复用 escalation / policy，别新建 |
| 7.5 | 拆解层的人的入口 | 小-中 | `HumanGate.review()` 现签名是 `(spec, signals, verdict, reason)`，装不下拆解复核，要么重载要么加方法 |

**`runtime/`、`orchestrator.py`、信号协议、checkpoint、恢复模式一行都不用动。** 改动集中在 `agent/architect.py`、`llm/` 的协议层、和一个新的拆解入口。

#### 顺序：先验证前提，再建生成侧

**7.1 + 7.2 先做**（最便宜），因为它直接验证整个阶段的前提：**独立复核到底有没有用**。前提不成立的话，生成侧就该换个设计，不该先建完再发现。

#### 实测要求（这条是硬性的）

M5b 那个 10/10 是**同模型**测出来的，不能外推到跨模型。跨模型复核有一个同模型没有的失败模式：**假阳性**——复核者不共享生成者的上下文，很可能对本来没问题的拆解报缺口，代价是白跑一轮重生成或白打扰人一次。

按 §11.9c 刚踩过的坑（第一版 ABANDON 判据在不可解任务上 12%→96% 看着是大胜，可解任务完成率同时从 81% 塌到 56%），**必须两侧都测**：

- 完整拆解上的**假阳性率**（复核者不该报缺口却报了）
- 缺陷拆解上的**召回率**（复核者该报却没报）

只测一侧一定会得出错误结论。参照 M5a 的口径出 TPR / FPR / Youden J，与同模型基线（M5b 的 10/10）对比。

**里程碑出口**：

1. 生成者能从一个自然语言目标产出可执行的子任务集，且通过 `plan.deterministic_review()`；
2. 跨模型复核在**两侧**都有数据，且 Youden J 优于同模型基线；
3. 生成-复核循环有确定性上限，超限升级给人；
4. 风险 #3 / #14 的状态按实测更新。

**预算参考**：按 M5 的经验，实测部分约 150 次运行 / 2.8M token——**这块比写代码贵**，提前算进去。

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
