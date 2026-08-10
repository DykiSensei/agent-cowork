# AGENTS.md

给 AI 编码代理的项目指南。假设读者对本项目一无所知。

## 项目概览

**agent-cowork**：多 Agent 协作系统 v0.1 原型（Python ≥ 3.11），验证的核心链路是
**L0 硬信号 → 中断 → REBASE → 恢复**。架构师（Architect）是唯一写入决策点，
Subagent 是薄执行层，Runtime 是完全确定性的（不含任何 LLM 调用）。

**设计文档是主线，代码是它的实现。** 动代码前先看 `多Agent协作系统-开发文档.md`
（主线，设计与全部实测记录，文中 §x.y 均指它的章节），动完之后回去更新它。
另有 `M6-界面层接口.md`：给界面层的对外接口约定 —— 改动 `to_dict()` 的形状、
`HumanGate` 的签名或信号类型时，那份文档必须同步改。

当前进度：M0–M7 全部完成；M8（写入侧复核，改 TaskSpec 前让复核者看一眼）代码完成
但**默认关闭** —— 判别力还没实测，跑 `cli bench-decide` 出 TPR/FPR/J 之前不要默认开。**M6 群聊界面层已落地**：前端在 `ui/`（React + TS +
Vite 双模式界面 + 设置页），服务层在 `src/cowork/server/`（FastAPI，
`python -m cowork.cli serve` 起服务，含 AWAITING_HUMAN 的 restore 路径）。
`CLAUDE.md` 是给 Claude Code 的同类指南，内容更细，两者应保持一致。

## 技术栈与运行架构

- **零必需依赖**是刻意的：`pyproject.toml` 的 `dependencies = []`。
  `anthropic` / `openai` / `psycopg` 都是可选 extra，且全部在函数内延迟导入。
  加依赖前先想清楚值不值。
- src layout（`src/cowork/`），setuptools 构建，入口 `cowork = cowork.cli:main`。
- 存储：SQLite（默认，零依赖，`store/sqlite.py`）/ Postgres（正式，`store/postgres.py`，
  建表见 `schema.sql`，五张表 + 两条 checkpoint 结构 CHECK）。
- 沙箱：本地白名单 / Docker 容器（workspace 整体只读挂载 + scope 内路径可写覆盖 +
  `--network none`）。
- 模型后端：统一 `Backend` 协议（`llm/__init__.py`）。`anthropic_backend.py` 走
  Anthropic Messages 方言；`openai_compat.py` 覆盖其余 9 家 OpenAI 兼容端点
  （预设表在 `cli.PROVIDERS`）；`scripted.py` 是确定性测试后端。
  **模型不走 SDK 的工具调用循环**，用结构化输出直接返回「下一个动作」，循环归我们持有。
- 基础设施（`docker-compose.yml`）：Postgres（宿主机 5433）+ LiteLLM 代理（4000），
  后者用 virtual key 承担 `token_budget` 的硬强制。`.env` 同时喂给 compose 和应用
  （格式必须 compose 兼容：KEY=value，不写 export）。

## 构建与测试命令

```bash
pip install -e .                                  # 或 set PYTHONPATH=src（仅 CLI 需要）

docker compose up -d postgres litellm             # postgres:5433 / litellm:4000
python -m unittest discover -s tests -t .         # 377 个测试。用 unittest，没引 pytest

python -m unittest tests.test_preemption                              # 单个文件
python -m unittest tests.test_chain.TestChain.test_rebase_cleared_the_trace  # 单个用例

python -m cowork.cli demo                         # SQLite + 本地沙箱 + 脚本后端
python -m cowork.cli demo --store pg --docker     # Postgres + Docker 沙箱
python -m cowork.cli demo --backend deepseek      # 真实模型（9 家见 cli.PROVIDERS）
python -m cowork.cli models                       # 拿各家 /v1/models 对 PROVIDERS 表
python -m cowork.cli composite                    # M4 复合任务：并行 + 冲突检测
python -m cowork.cli plan "<一句话目标>"           # M7：拆解 + 复核；加 --run 一路跑到产出

python -m cowork.cli bench --backend deepseek --repeat 5   # M2 跑批，约 25 分钟 / 1.6M token
python -m cowork.cli bench-report bench_runs.jsonl         # 只出报告，不重跑不花钱
python -m cowork.cli bench-review / bench-plan ...         # M7 复核 / 拆解对照实测

pip install -e .[server]                                   # M6 服务层依赖（fastapi/uvicorn）
python -m cowork.cli serve                                 # HTTP + SSE + 静态 UI（只绑 loopback）
```

- 测试不需要 `PYTHONPATH`：`tests/__init__.py` 负责挂 `src/` 并载入 `.env`。
- 三样基础设施（Postgres / LiteLLM / Docker 守护进程）都不起时，依赖它们的 14 个
  测试会 skip（3 PG + 5 LiteLLM + 6 Docker 沙箱），不是失败。
- 打真实供应商的用例需要 `.env` 里配 `DEEPSEEK_API_KEY` 或 `MOONSHOT_API_KEY`。
- **跑批要花真钱**（bench 约 25 分钟），先 `--tasks p1_word_count --repeat 1` 冒烟。
  仓库根的 `*.jsonl` 是历次跑批原始记录，是 `policy.py` 全部参数的依据，
  `bench-report <文件>` 可随时重出报告。
- Windows 上中文输出用 PowerShell，Git Bash 控制台会乱码。

## 代码组织（代码地图）

```
types.py        数据结构，TaskSpec 硬约束在 __post_init__
actions.py      Subagent 每 step 只能产出三种动作：ToolCall / Finish / SoftSignalAction
signals.py      信号协议 + task_class → 硬信号覆盖面
policy.py       全部待验证参数集中在这里（都有实测依据），不要散落成魔数
escalation.py   确定性升级下限，执行层与拆解层共用
resume.py       RESUME / REBASE / RESTART
orchestrator.py 状态机 + PROBE 分段
plan.py         拓扑分层 / 可分解性 / 静态冲突 + 结构性复核 —— 全确定性无 LLM
scheduler.py    并行调度 + 产出层冲突检测 + 仲裁
config.py       .env 加载（环境变量优先，空值=未设置）+ redact
cli.py          全部子命令 + PROVIDERS 预设表 + DEFAULT_REVIEWER
runtime/        确定性层：bus / sandbox / detectors / loop —— 这里不许出现 LLM 调用
agent/          architect（唯一写入决策点）/ subagent（薄绑定层）
llm/            __init__ 是后端协议本身（给模型加能力从这里改）；
                scripted / anthropic_backend / openai_compat / errors
store/          sqlite（默认）/ postgres
server/         M6 服务层：app（路由）/ runner（线程编排 + plan 注册表）/
                gate（ChatGate）/ tap（写入处发事件）/ settings_io（.env 读写）/
                bind（绑定地址准入：非回环拒绝启动）
views.py        界面层投影：thread_list / task_detail / pending_ruling（服务层只调它）
bench/          实测工具，只包装不改被测对象，不参与生产链路
demo*.py        验证场景，「隐藏要求」写在这里
```

界面层（M6 前端）在仓库根的 `ui/`：React 18 + TS + Vite，`npm run dev / build /
preview`；mock API（`ui/mock/plugin.ts`）按 `M6-界面层接口.md` §6 契约应答，
数据骨架是 `ui/fixtures/` 里的真实 CLI 输出。双模式（简洁默认 / 专业），
lite 的术语翻译集中在 `ui/src/copy.ts`。细节见 `ui/README.md`。

一次 run 的控制流（跨四个文件，先读这段再进代码）：
`Orchestrator.run()`（最多 `max_cycles=8` 轮）→ `StepLoop.run()`（循环开头
`take_preempt()` 是外部抢占的全部实现）→ 硬信号 → `architect.decide()` →
`apply_resume()` → 新 spec / 新 Sandbox / 新 StepLoop。硬信号只在 `loop.py` 的
`interrupted()` 里产生，加信号类型从那里入手。

## 四条架构不变量（改动不能破坏，有测试守着）

| 不变量 | 守护测试 |
|---|---|
| 执行层中心化，Subagent 之间无通信 API | 结构性（不要新增这类接口）+ `test_scheduler` |
| Runtime 不含 LLM，硬信号全部确定性产生 | `test_chain` |
| step 循环自持，外部抢占 = 循环开头一次状态检查 | `test_preemption` |
| checkpoint 里 `produced` / `reasoning_trace` 是两个顶层键 | `test_chain` + Postgres CHECK |

第三条是地基：**控制流自己写，基础设施才外购。** 不要引入替我们决定
「谁下一个执行」的框架。

## 开发约定（代码风格）

- **注释和文档用中文**，与现有风格一致。注释解释「为什么」，不复述代码。
- **任务做完就更新设计文档**，不要停在「要我更新吗」。文档滞后 = 主线失真。
- **文档里不写行号**，按符号引用（如 `loop.py` 的 `interrupted()`）。
- `policy.py` 的参数全部有实测依据，改它们之前先跑 `bench` 系列，别凭感觉调。
- 拆解层的特殊约束（详见 CLAUDE.md）：只有生成者有写权；模型只填它有权决定的字段
  （goal / 验收标准 / scope / 依赖），sandbox 与上限走 `SpecTemplate`；
  复核者默认 `kimi`，`--reviewer none` 退回同模型复核。

## 测试策略

- 377 个 unittest 用例，默认全本地可跑（脚本后端是确定性的）。
- **同一场景跨运行方差很大**（中断 0–5 次，token 0–50k）：单次运行不能作为
  参数或结论的依据，**也不能拿它写断言**。
- **改提示词必须两侧都测**（正例 + 反例）：M5a 第一版只看不可解侧是「大胜」，
  可解侧完成率其实塌了 —— 是偏置移动不是判别力提升。根目录的 `m5a_*.jsonl` /
  `review_ab*.jsonl` / `plan_ab.jsonl` 是现成基线。
- 确定性护栏是兜底不是主力，别拿护栏命中数当改动生效的证据。
- 对照实验的负例最容易是自己写错；但也别改到 FPR 归零（那是在拟合测试集）。

## 安全注意事项

- `.env` 已 gitignore（从 `.env.example` 复制）。提交前确认未进暂存区
  （`git ls-files | grep '^\.env$'` 应为空）。环境变量优先于文件，空值视为未设置。
- 密钥不进三个地方：**命令行参数**（CLI 不接受 key 参数）、**日志**、**数据库**
  （`signals.raw_evidence` 存 provider 原始错误体，在 `SignalBus.emit()` 统一脱敏）。
- Docker 沙箱只挂载任务 workspace、不挂载项目根，`.env` 对 Subagent 不可见。
  **不要图方便把项目根挂进容器。**
- LiteLLM 预算拒绝是 HTTP 429，与真实限流同码 —— 必须看错误体的 `error.type`
  判断（`llm/errors.py`），不能按状态码。
- 沙箱越权提级只匹配「只读文件系统」这类明确的内核拒绝，**不要**匹配泛化的
  `Permission denied`（会把应用自身的权限错误误判成越界）。

## 主要踩坑（详单见 CLAUDE.md「踩过的坑」）

- **`events` 表上不能有外键**：事件是线程级的，而复合任务的 root 线程按设计没有
  `tasks` 行。加了外键 → PG 上复合线程时间线全空且零报错（异常被合理地吞掉了），
  SQLite 不强制外键所以测试全绿。
- **取消不走架构师**：`intervene` 交回控制权，架构师可能回 `CONTINUE`，人的取消
  会降级成建议。`Orchestrator.cancel()` 抢占后直接 `ABANDONED`。
- 抢占队列必须在中断时清空，否则一次中断放大成无限中断。
- 模型调用失败要变成硬信号，不能抛异常穿透整个 run。
- 硬信号是「任务级失败」，探测不存在文件返回 `ok=False` 不算（靠
  `ToolResult.hard_failure` 区分）。
- 解析失败抛 `ModelError` 走硬信号通道，别抛 `ValueError`。
- 子进程输出固定 `encoding="utf-8", errors="replace"`，不能按系统编码解码
  （中文 Windows 上 GBK 会在读取线程里炸掉）。
- 提示词拼装顺序 = 缓存命中率：静态在前、可变在后，这是条沉默的不变量
  （功能测试全绿但命中率归零），有 `test_openai_compat.TestPromptCaching` 钉着。
- 并行要求存储层可并发：SQLite 已改 `check_same_thread=False` + 方法级 `RLock`，
  锁必须覆盖到 `fetch`。
- 给循环加「让出控制权」时，以循环为计量单位的东西（`max_steps` / `deadline_s`）
  都要跟着走。
