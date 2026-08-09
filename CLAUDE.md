# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

多 Agent 协作系统原型。**设计文档是主线，代码是它的实现** —— 动代码前先看
`多Agent协作系统-开发文档.md`，动完之后回去更新它。

当前：M2 / M3 / M4 已完成，下一步 M5a（架构师的停止判断）。路线图见文档 §12。
`policy.py` 的参数已有实测依据（§11.6 / §11.7），改它们之前先跑 `bench`，别凭感觉调。

## 命令

```bash
docker compose up -d postgres litellm     # postgres:5433 / litellm:4000
                                          # 不起的话 14 个测试会 skip（不是失败）
python -m unittest discover -s tests -t . # 147 个测试。项目用 unittest，没引 pytest

python -m unittest tests.test_preemption                              # 单个文件
python -m unittest tests.test_chain.TestChain.test_rebase_cleared_the_trace  # 单个用例
python -m unittest discover -s tests -t . -v                          # 看每个用例名

python -m cowork.cli demo                       # SQLite + 本地沙箱 + 脚本后端
python -m cowork.cli demo --store pg --docker   # Postgres + Docker 沙箱
python -m cowork.cli demo --backend deepseek    # 真实模型（也可 kimi / anthropic / openai）
python -m cowork.cli demo --json                # 每行一条 JSON，末尾一份完整结果
python -m cowork.cli inspect <db.sqlite>        # 导出某个库的任务与 DecisionRecord
python -m cowork.cli composite                  # M4 复合任务：并行 + 冲突检测

python -m cowork.cli bench --backend deepseek --repeat 5  # M2 跑批，约 25 分钟 / 1.6M token
python -m cowork.cli bench --tasks PROBE_AB --repeat 5    # M3 的 PROBE vs TRUST 对照
python -m cowork.cli bench-report bench_runs.jsonl        # 只出报告，不重跑
```

`bench` 要花真钱和 25 分钟，跑之前先用 `--tasks p1_word_count --repeat 1` 冒烟。

测试不需要 `PYTHONPATH` —— `tests/__init__.py` 负责挂 `src/` 并载入 `.env`
（所以打真实供应商的用例能自己拿到 key）。CLI 未 `pip install -e .` 时才需要
`PYTHONPATH=src`（src layout）。Windows 上中文输出用 PowerShell，
Bash 工具的控制台会乱码。

环境变量（全部可选，真实环境变量优先于 `.env`）：

| 变量 | 作用 |
|---|---|
| `COWORK_PG_DSN` | Postgres 连接串，默认 `postgresql://cowork:cowork@localhost:5433/cowork` |
| `COWORK_LLM_BASE_URL` / `COWORK_LLM_API_KEY` | 指向 LiteLLM 或任意 OpenAI 兼容端点 |
| `COWORK_ARCHITECT_MODEL` / `COWORK_SUBAGENT_MODEL` / `COWORK_TRIAGE_MODEL` | 覆盖 `cli.py` 的 `PROVIDERS` 默认分工 |
| `COWORK_ENV_FILE` | 换一份 `.env` |

## 四条架构不变量 —— 改动不能破坏

| 不变量 | 守护它的测试 |
|---|---|
| 执行层中心化，Subagent 之间无通信 API | 结构性（不要新增这类接口）+ `test_scheduler` |
| Runtime 不含 LLM，硬信号全部确定性产生 | `test_chain` |
| step 循环自持，外部抢占 = 循环开头一次状态检查 | `test_preemption` |
| checkpoint 里 `produced` / `reasoning_trace` 是两个顶层键 | `test_chain` + Postgres CHECK |

第三条是整个设计的地基（文档 §10.1）：**控制流自己写，基础设施才外购**。
不要引入替我们决定「谁下一个执行」的框架。模型也不走 SDK 的工具调用循环 ——
用结构化输出直接返回「下一个动作」，循环归我们持有。

## 一次 run 的控制流

读懂这条链要跨四个文件，先看这里再进代码：

```
Orchestrator.run()  最多 max_cycles=8 轮，每轮换一个新 Subagent 实例
  └─ StepLoop.run()          ← 循环开头 take_preempt()：外部抢占的全部实现
       每 step：next_step() → ToolCall 走 Sandbox / SoftSignal 入队 / Finish 收尾
       Finish 时还要过两关：validate_schema → VALIDATION_FAILED
                            机器可检的 acceptance → TEST_FAILED
       任何硬信号 → interrupted()：清空抢占队列、落 checkpoint、带信号返回
  └─ 回到 Orchestrator：
       COMPLETED → architect.verify()，不通过就自造 VALIDATION_FAILED 再中断
       INTERRUPTED → architect.decide() → DecisionRecord
       → apply_resume(RESUME / REBASE / RESTART) → 新 spec、新 Sandbox、新 StepLoop
```

几个只有读代码才发现的点：

- **硬信号只在 `interrupted()` 里产生**，所以 `loop.py` 里 `return interrupted(...)`
  的每一处就是硬信号的完整清单，加信号类型从这里入手。
- **决策权分两段**：`escalation.should_escalate()` 先做不经 LLM 的确定性判断
  （超中断上限、scope 越界、顶层 MODIFY_TASK、不可逆命令…），命中就交 `HumanGate`；
  没命中才看 LLM 自评的 `complexity_score` 是否低于 `policy.complexity_threshold`。
- **恢复模式不是模型选的**，`resume.choose_resume_mode()` 按新旧 spec 的差异算：
  goal 没变 → RESUME / REBASE，goal 实质变了且 produced 无用 → RESTART。
- **三种收尾都不是异常**：`ABANDON` → ABANDONED；架构师调不动模型、缺 `resume_mode`、
  REBASE 超 `policy.max_rebase` → `AWAITING_HUMAN`；跑满 max_cycles → FAILED。
- **PROBE 是「让出控制权」而不是中断**（M3）。`loop.py` 只判「到间隔了」这个确定性
  事实，返回 `probe_due=True`；判不判得出跑偏归架构师。在轨就接着跑，不消耗
  cycle、不换 Subagent；跑偏才由 **Orchestrator**（不是 Runtime）发
  `VALIDATION_FAILED` + `payload.origin="architect_probe"`，汇进上面那条既有链路。
- **复合任务多一层**：`Scheduler` 按 `plan.build_plan()` 分层，层内并行跑多个
  `Orchestrator`。冲突检测在层与层之间做，仲裁仍然走 `Architect.decide()`。

## 约定

- **任务做完就更新文档**，不要停在「要我更新吗」。文档滞后 = 主线失真。
- **文档里不写行号**，按符号引用（`loop.py` 的 `interrupted()`）。行号必然漂移。
- 注释和文档用中文，与现有风格一致。注释解释「为什么」，不复述代码。
- **零必需依赖**是刻意的。`anthropic` / `openai` / `psycopg` 都是可选 extra，
  且都在函数内延迟导入。加依赖前先想清楚值不值。
- 待调参数集中在 `policy.py`，不要散落成魔数。
- 提交前确认 `.env` 未进暂存区（`git ls-files | grep '^\.env$'` 应为空）。

## 踩过的坑

这些都真实花过时间，改相关代码时留意：

- **抢占队列必须清空**。中断时只取走触发的那条硬信号，队列里剩下的会在下一轮
  循环开头再次抢占，把一次中断放大成无限中断。
- **Docker 沙箱的隔离靠只读挂载**，不是靠工具层白名单 —— `run` 能执行任意代码，
  白名单管不住它。workspace 整体 `:ro`，scope 内路径可写覆盖。
  提级判定只匹配「只读文件系统」这类明确的内核拒绝，**不要匹配泛化的
  `Permission denied`**，那会把应用自身的权限错误误判成越界。
- **LiteLLM 的预算拒绝是 HTTP 429，与真实限流同码**。必须看错误体的
  `error.type`，不能按状态码判断。分类逻辑在 `llm/errors.py`。
- **多供应商的配置回退 bug 只在两家 key 同时存在时显形**。改 provider 解析时，
  用两家 key 一起测（`test_openai_compat.TestProviderResolution` 就是为这个建的）。
- **模型调用失败要变成信号，不能抛异常穿透** —— 否则架构师连中断决策的机会
  都没有。共用一把耗尽的 key 时架构师也会挂，此时正确行为是 `AWAITING_HUMAN`。
- **demo 场景的「隐藏要求」必须真的不可推断**。早期版本用「需要归一化大小写
  与标点」，真实模型直接写对，三次运行零中断，场景失去区分度。设计任何验证
  场景时先问：这个失败真实模型会不会自己避开？
- **同一场景跨运行方差很大**（M2 的 75 次运行里中断 0–5 次，token 0–50k）。
  单次运行不能作为任何参数或结论的依据，**也不能拿它写断言** ——
  `test_full_chain_with_real_model` 原来断言「4 轮内必然 COMPLETED 或
  AWAITING_HUMAN」，实测证明 FAILED 也是设计内的终局，那条断言只是个偶发红灯。
- **硬信号是「任务级失败」，不是「任何非零返回」**。探测一个还不存在的文件返回
  `ok=False`，曾被判成 `TOOL_FAILURE` 抢占 —— 每个任务开局白烧一轮架构师决策。
  现在靠 `ToolResult.hard_failure` 区分。加新工具时想清楚它的失败算哪一类。
- **schema 校验通过不等于语义有效**。`ACTION_SCHEMA` 用空串表示「本字段不适用」，
  于是 `kind=tool_call` + `tool=""` 能过校验再往下炸。解析失败必须抛 `ModelError`，
  走硬信号通道 —— 和模型调用失败同一条路，别抛 `ValueError`。
- **设计验证场景时，验收命令对 Subagent 是可执行的**。它会自己跑一遍，
  失败信息里的期望值等于把答案告诉它。`bench/tasks.py` 的用例表因此存成压缩 blob，
  但这挡不住「跑一次就知道」——想验「架构师改规格」的链路要另想办法。
- **给循环加「让出控制权」时，以循环为计量单位的东西都要跟着走**。M3 的 PROBE
  分段一度让 `max_steps` / `deadline_s` 每段清零，而那正是 GENERATIVE 仅剩的硬信号。
  现在靠 `cycle_steps_used` / `cycle_started` 跨段传递。另外「无进展就不探查」
  是必需护栏，否则会「探查 → 无进展 → 再探查」空转烧 token。
- **跨组比中位数时先控住高方差项**。M3 的 PROBE 表面溢价 3.4x，差点触发文档里
  「>3x 就重新设计」的判断点；拆开发现差额来自各 arm 抽到的中断次数不同，
  控住之后是 1.45x。单次运行是噪声，**没控变量的组间中位数比较同样是噪声**。
- **并行要求存储层可并发**。`sqlite3` 连接不是线程安全的，现在是
  `check_same_thread=False` + 方法级 `RLock`，锁必须覆盖到 `fetch`
  （只锁 `execute` 的话游标会在锁外回头碰连接）。

## 密钥

`.env`（已 gitignore，从 `.env.example` 复制）。环境变量优先于文件，空值视为未设置。
测试会自动加载。密钥不进三个地方：命令行参数、日志、数据库 ——
第三条靠 `SignalBus.emit()` 里的 `redact()`，因为 `signals.raw_evidence` 存的是
第三方错误体且会长期留在 Postgres 里。

容器只挂载任务 workspace、不挂载项目根，所以 `.env` 对 Subagent 不可见。
**不要为了图方便把项目根挂进容器。**

## 代码地图

```
types.py        §4 数据结构，TaskSpec 的硬约束在 __post_init__
signals.py      §3 信号协议 + task_class → 硬信号覆盖面
policy.py       §9 待验证参数
escalation.py   §7.2 不经 LLM 的确定性升级下限
resume.py       §6 RESUME / REBASE / RESTART
orchestrator.py §5 状态机 + PROBE 分段（M3）
plan.py         §12 M4 拓扑分层 / 可分解性 / 静态 scope 冲突 —— 全确定性
scheduler.py    §12 M4 并行调度 + 产出层冲突检测 + 仲裁
runtime/        确定性层：bus / sandbox / detectors / loop —— 这里不许出现 LLM 调用
agent/          architect（唯一写入决策点）/ subagent（薄绑定层）
llm/            scripted（确定性测试用）/ anthropic_backend / openai_compat / errors
store/          sqlite（默认）/ postgres（正式）
bench/          §12 M2/M3 实测 —— 只包装不改被测对象，不参与生产链路
```
