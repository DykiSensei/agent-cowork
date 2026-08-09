# CLAUDE.md

多 Agent 协作系统原型。**设计文档是主线，代码是它的实现** —— 动代码前先看
`多Agent协作系统-开发文档.md`，动完之后回去更新它。

当前：M1 已完成，下一步 M2（参数实测）。路线图见文档 §12。

## 命令

```bash
docker compose up -d postgres litellm     # 起服务；不起的话 14 个测试会 skip（不是失败）
python -m unittest discover -s tests -t . # 82 个测试。项目用 unittest，没引 pytest

python -m cowork.cli demo                       # SQLite + 本地沙箱 + 脚本后端
python -m cowork.cli demo --store pg --docker   # Postgres + Docker 沙箱
python -m cowork.cli demo --backend deepseek    # 真实模型（也可 kimi / anthropic / openai）
```

没装包时用 `PYTHONPATH=src`（src layout）。Windows 上中文输出用 PowerShell，
Bash 工具的控制台会乱码。

## 四条架构不变量 —— 改动不能破坏

| 不变量 | 守护它的测试 |
|---|---|
| 执行层中心化，Subagent 之间无通信 API | 结构性（不要新增这类接口） |
| Runtime 不含 LLM，硬信号全部确定性产生 | `test_chain` |
| step 循环自持，外部抢占 = 循环开头一次状态检查 | `test_preemption` |
| checkpoint 里 `produced` / `reasoning_trace` 是两个顶层键 | `test_chain` + Postgres CHECK |

第三条是整个设计的地基（文档 §10.1）：**控制流自己写，基础设施才外购**。
不要引入替我们决定「谁下一个执行」的框架。模型也不走 SDK 的工具调用循环 ——
用结构化输出直接返回「下一个动作」，循环归我们持有。

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
- **同一场景跨运行方差很大**（中断 0–3 次，token 4.2k–23.2k）。
  单次运行不能作为任何参数或结论的依据。

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
orchestrator.py §5 状态机
runtime/        确定性层：bus / sandbox / detectors / loop —— 这里不许出现 LLM 调用
agent/          architect（唯一写入决策点）/ subagent（薄绑定层）
llm/            scripted（确定性测试用）/ anthropic_backend / openai_compat / errors
store/          sqlite（默认）/ postgres（正式）
```
