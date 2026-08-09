# 多 Agent 协作系统 —— 原型

对应开发文档 §12 路线图。

```
M0 ✅ 核心链路验证    L0 硬信号 → 中断 → REBASE → 恢复
M1 ✅ 真实环境收口    Postgres / Docker 沙箱 / 真实模型 / virtual key 预算强制
M2 ◻ 参数实测        ← 下一步
```

---

## 跑起来

```bash
docker compose up -d postgres litellm
python -m unittest discover -s tests -t .
```

82 个测试。不起 Docker 的话，依赖真实服务的 14 个会 skip，其余照常跑；
另有 2 个真实供应商测试需要 `DEEPSEEK_API_KEY` 或 `MOONSHOT_API_KEY`（`.env` 里配好即可，测试会自动读）。

```bash
python -m cowork.cli demo                      # SQLite + 本地沙箱 + 脚本后端
python -m cowork.cli demo --store pg --docker  # Postgres + Docker 沙箱
python -m cowork.cli demo --backend deepseek   # 真实模型（需要 key，见下）
```

（未安装时先 `pip install -e .`，或 `set PYTHONPATH=src`。）

演示场景的实际输出：

```
[RUN ] cycle=1 rev=1 agent=agent_… step=0
[STOP] TEST_FAILED @step=2 interrupt_count=1
[DEC ] decider=HUMAN action=MODIFY_TASK resume=REBASE complexity=0.25
       升级原因: 要对 parent_id 为空的顶层任务执行 MODIFY_TASK
       理由: verify.py 的失败用例集中在含大小写与标点的输入上，
             说明原 TaskSpec 没有把归一化要求写进验收标准。补一条，goal 不变。
[RUN ] cycle=2 rev=2 agent=agent_… step=2
[DONE]

最终状态 COMPLETED / revision 2 / 中断 1 次
信号流水 L0 TEST_FAILED  RUNTIME  PREEMPTED
```

故事线对应 MAST 里「规格不清 42%」那一类失败。

---

## 四条架构不变量（有测试守着）

| 不变量 | 落地位置 | 测试 |
|---|---|---|
| 执行层中心化，Subagent 只与架构师通信 | 没有 Subagent 间通信的 API 面 | 结构性保证 |
| Runtime 不含 LLM，硬信号全部确定性产生 | `runtime/` 整个包无模型调用 | `test_chain` |
| step 循环自己持有，抢占 = 不派发下一个 step | `runtime/loop.py` 循环开头一次状态检查 | `test_preemption` |
| checkpoint 里 produced / reasoning_trace 是两个顶层键 | `types.AgentContext` + DB CHECK | `test_chain` + `test_postgres_store` |

---

## M1 实测结论

### 1.1 Postgres ✅

`schema.sql` 由 initdb 自动建表，五张表全部落地。DB 层的两条 CHECK 生效：

```sql
CONSTRAINT ctx_has_produced        CHECK (context_json ? 'produced'),
CONSTRAINT ctx_has_reasoning_trace CHECK (context_json ? 'reasoning_trace')
```

故意写扁平 context 会被 `CheckViolation` 拒掉，而不是等到 REBASE 时才发现。

REBASE 语义在 Postgres 上直接可查：

```
 step | produced | trace | rev
    2 |        1 |     3 | 1     ← rev1 末尾
    3 |        2 |     2 | 2     ← REBASE 后：产出保留，推理痕迹清空重起
```

### 1.2 Docker 沙箱 ✅

**出口标准要堵的洞**：原来 `-v {workspace}:/w` 整目录可写，`run` 执行任意代码能绕过工具层白名单。测试 `test_local_sandbox_does_not_contain_run` 把这个事实钉住了 —— 本地模式下 `run` 确实能改 scope 外的文件。

现在的做法：workspace 整体**只读**挂载，再把 scope 内的具体路径以可写方式覆盖上去。

```
-v {workspace}:/w:ro
-v {workspace}/solution.py:/w/solution.py
--network none
```

于是越权写入在**内核层面**被拒（`OSError: [Errno 30] Read-only file system`），
Runtime 把这类失败提级为 `SCOPE_VIOLATION` 而不是语焉不详的 `TOOL_FAILURE`。
只匹配「只读文件系统」这类明确的内核拒绝，**不匹配**泛化的 `Permission denied`
—— 那会把应用自身的权限错误误判成越界。

bind mount 要求源文件已存在，所以无通配符的 scope 项若不存在会先建空文件。
这些路径本就在 scope 内，创建它们不构成越界。

### 1.4 virtual key 预算强制 ✅

文档标的返工风险确认存在：**LiteLLM 的预算拒绝用 HTTP 429，与真实限流同码**，
不能靠状态码判断。实测形态（litellm main-latest，2026-08）：

```
HTTP 429
{"error":{"message":"Budget has been exceeded! Key=… Current cost: 1.0, Max budget: 0.05",
          "type":"budget_exceeded","param":null,"code":"429"}}
```

转换层落在 `llm/errors.py`（它是 provider 错误分类，与 Runtime 的确定性检测器
`detectors.py` 职责不同）。优先匹配结构化的 `error.type`，文案串兜底，
并有反例护栏挡住 401 鉴权错误。

端到端验证的是这句话：**应用层的 `token_budget` 是软限制，virtual key 才是硬限制**。
`test_budget_end_to_end` 里应用层预算故意留 1000 万 token，代理照样拒，链路照样中断。

顺带补上一个真实缺口：模型调用失败原先会让整个 run 崩，架构师连中断决策的机会都没有。
现在归类成硬信号交给架构师；如果架构师自己也调不动模型（典型场景：与 Subagent 共用
一把耗尽的 key），挂起等人 —— 没有决策者时不猜。

### 1.3 真实模型 ✅

DeepSeek（架构师 `deepseek-reasoner` / Subagent 与分诊 `deepseek-chat`）和
Kimi（`kimi-k3`）都跑通了完整链路，产出的 `solution.py` 经独立复核正确。

**最有价值的发现：demo 场景对真实模型失去了区分度。**
原场景把「需要归一化大小写与标点」当作隐藏要求，脚本后端靠脚本强制写出朴素实现，
必然触发 `TEST_FAILED`。真实模型直接写对了 —— 连续三次零中断，链路一次没被触发。

问题不在模型太强，在场景设计：**一个能被模型推断出来的「隐藏要求」，
根本不是规格缺失**。已换成真正不可推断的项目约定（本项目约定空串不算回文，
与通行理解相反）。改完后脚本后端和真实模型走同一条链路。

其它实测数据（详见开发文档 §11.5）：

| 观察 | 数据 |
|---|---|
| 真实模型的失败路径 | `TEST_FAILED` / `SCOPE_VIOLATION` / `TOOL_FAILURE`，脚本后端只有一种 |
| 中断次数 ↔ token | 0 次 4–5k，1 次 7–9k，2 次 18k，3 次 23k |
| 同场景运行间方差 | 8 次运行，中断 0–3 次，token 4.2k–23.2k |

最后一条直接影响 M2：**单次运行不能作为任何参数的依据**，每个任务至少跑 5 次取分布。

---

## 目录

```
schema.sql              §10.5 五张表
initdb/                 首次初始化时建 litellm 库
docker-compose.yml      postgres（5433）+ litellm（4000）
litellm.config.yaml     §10.3 模型路由
src/cowork/
  types.py              §4 数据结构；TaskSpec 硬约束在 __post_init__
  signals.py            §3 信号协议 + task_class → 硬信号覆盖面（§3.2.1）
  policy.py             §9 待验证参数集中在这里
  escalation.py         §7.2 不经 LLM 的确定性升级下限
  resume.py             §6 RESUME / REBASE / RESTART
  orchestrator.py       §5 状态机
  runtime/
    bus.py              L0 抢占通道 + L1 队列
    sandbox.py          工具执行 + scope 强制（本地白名单 / 容器只读挂载）
    detectors.py        output_schema 校验
    loop.py             step 循环 —— 控制流核心
  agent/
    subagent.py         薄绑定层：智能在 backend，权限在 runtime
    architect.py        决策 / 软信号分诊 / 验收 / 人的介入口
  llm/
    scripted.py         确定性后端
    anthropic_backend.py 真实后端
    errors.py           provider 错误 → L0 硬信号
  store/
    sqlite.py           零依赖，默认
    postgres.py         §10.2 正式选型
```

---

## 接真实模型

两个后端，同一个 `Backend` 协议：

| 后端 | 方言 | 供应商 | CLI |
|---|---|---|---|
| `llm/anthropic_backend.py` | Anthropic Messages | Claude 全系 | `--backend anthropic` |
| `llm/openai_compat.py` | OpenAI Chat Completions | DeepSeek、Kimi(Moonshot)、任何 OpenAI 兼容端点 | `--backend deepseek` / `kimi` / `openai` |

### 配置密钥

```bash
cp .env.example .env      # 已在 .gitignore 里
# 编辑 .env，填 DEEPSEEK_API_KEY 或 MOONSHOT_API_KEY
```

真实环境变量优先于 `.env`，容器 / CI 覆盖不受影响。
格式是 docker compose 兼容的，所以同一份 `.env` 同时喂给 compose 和应用。

密钥不进三个地方：**命令行参数**（会进 shell history 和进程列表，所以 CLI 不接受 key 参数）、
**日志**（`load_env()` 只返回键名）、**数据库**（`signals.raw_evidence` 存的是 provider
原始错误体，在 `SignalBus.emit()` 这个唯一入口统一脱敏）。

第三条容易被忽略，因为链条是间接的：模型调用失败 → 错误体成为硬信号证据 →
信号长期留在 Postgres。容器只挂载任务 workspace 而非项目根，所以 `.env` 对 Subagent 不可见。

### 直连供应商（最快）

```bash
pip install openai
python -m cowork.cli demo --backend deepseek
python -m cowork.cli demo --backend kimi
```

默认分工：架构师 `deepseek-reasoner`，Subagent 与分诊 `deepseek-chat`。
用 `COWORK_ARCHITECT_MODEL` / `COWORK_SUBAGENT_MODEL` / `COWORK_TRIAGE_MODEL` 覆盖。

### 经 LiteLLM（要 virtual key 的预算强制时）

```bash
DEEPSEEK_API_KEY=sk-... docker compose up -d litellm

curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-cowork-master" -H "Content-Type: application/json" \
  -d '{"models":["deepseek-chat","deepseek-reasoner"],"max_budget":5.0,"key_alias":"task-xxx"}'

COWORK_LLM_BASE_URL=http://localhost:4000/v1 \
COWORK_LLM_API_KEY=sk-<上面返回的 virtual key> \
python -m cowork.cli demo --backend openai
```

`litellm.config.yaml` 里已配好 claude / deepseek / kimi 的模型组，按需增删。

### 为什么不让 LiteLLM 翻译 Anthropic 请求

实测确认代理的 `/v1/messages` **能**路由到 DeepSeek/Moonshot（请求到了上游，
回来的是各家自己的鉴权错误）。但我们依赖 `output_config.format`（Anthropic 专有
的结构化输出），能否被忠实翻译无法验证。**结构化输出被静默丢弃比不支持更糟** ——
Subagent 的动作解析会崩在一个看似正常的响应上。

所以 OpenAI 兼容后端直接说对方母语，并用三层保证 JSON，不赌供应商的 schema 支持：

1. system prompt 里写死 schema，要求只输出 JSON
2. 支持时带 `response_format={"type":"json_object"}`（`deepseek-reasoner` 不支持，退化为纯提示词）
3. 本地用 Runtime 的 `validate_schema` 校验，不合格带着错误再问一轮；仍不合格抛 `ModelCallFailed` → 硬信号

第 3 层是要害：**校验权留在 Runtime 侧**，和「Runtime 不含 LLM 但负责确定性校验」是同一条原则。

两个后端共用 `llm/errors.py` 的错误分类，所以实测过的预算强制对 DeepSeek/Kimi 同样成立。

模型不走 SDK 的工具调用循环，而是用结构化输出直接吐「下一个动作」：
**循环归我们持有**，模型只提供决策数据。

---

## 还没做

| 项 | 状态 |
|---|---|
| M2 参数实测 | `policy.py` 六个值仍是猜测。需先建 10–20 个任务的固定任务集，每个跑 ≥5 次取分布 |
| M3 `PROBE` 模式 | `Orchestrator` 显式抛 `NotImplementedError`，不半做 |
| M4 并行与冲突检测 | 未做（风险 #7） |
| M5 架构师自身验证 | 未做（风险 #3，文档自评最大缺口） |
| M6 群聊界面层 | 未做。当前是 CLI + `--json` 结构化日志 |
