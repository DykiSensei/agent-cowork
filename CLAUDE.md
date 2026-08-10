# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

多 Agent 协作系统原型。**设计文档是主线，代码是它的实现** —— 动代码前先看
`多Agent协作系统-开发文档.md`，动完之后回去更新它。
另有 `M6-界面层接口.md`：给界面层那一侧的接口约定。**改动 `to_dict()` 的形状、
`HumanGate` 的签名、或信号类型时，那份文档也要跟着改** —— 它是对外承诺。

当前：M2–M5 完成，**M7（拆解三角色）收口**，四条出口标准全部达成
（§11.11 / §11.12 / §11.13）。**下一步 M6（群聊界面层）**。

`policy.py` 的参数全部有实测依据（§11.6 / §11.7 / §11.9 / §11.13），
改它们之前先跑 `bench` 或 `bench-plan`，别凭感觉调。

拆解这一层的要点，动代码前先看 §12 M7 / §11.11 / §11.12 / §11.13：

- **只有生成者有写权**。复核者是顾问、人是仲裁者 —— §2.3 的「唯一写入决策点」不变。
  给复核者驳回或改写 spec 的能力 = 两个写入点 = 不变量破了（`test_decompose` 钉着）。
- **拆解层和执行层的循环同构**，判据因此放在 `escalation.deterministic_plan_escalation()`
  而不是架构师内部：生成→复核→重生成≤N→升级给人 ≙ 派发→验收→REBASE→超上限→升级给人。
  **发现自己在写平行逻辑就是方向错了。**
- **模型只填它有权决定的字段**：goal / 验收标准 / scope / 依赖。sandbox、工具白名单、
  各类上限走 `SpecTemplate` —— 让被隔离方给自己配隔离边界是没有意义的。
- **两层复核都不问「拆出来的东西合起来能不能跑」**（风险 #16）。结构层查交集与环，
  语义层查覆盖。真实生成的拆解已经栽过一次：模型用「一人一个子目录」满足 scope 不相交，
  依赖方 import 不到被依赖方。`isolated_dependency` 只堵了这一种形态。
- **「指纹重复」判据在拆解层几乎是死的**（§11.13）：16 次重生成里第二轮缺口
  16/16 都和第一轮不同，一次没触发，兜底全靠 `max_regenerate`。执行层的指纹看的是
  「同一个信号原样重现」，而复核者每轮看到的是一份**不同的**拆解 ——
  **判据移植过来了，但在新的一层上它没有可判之物**。别拿它当主力。
- **「复核一轮放行率」不是拆解质量的度量**（风险 #18）：更细的拆解给复核者更多
  可挑之处。限定词纪律那版 50% vs 朴素版 56%，但前者漏的是深层衔接、后者漏的是
  限定词本身 —— 两臂在不同水位线上被驳回。要真比质量得看派发执行后的产出。
- 复核者默认 `kimi`（`cli.DEFAULT_REVIEWER`）：§11.11 实测 J 0.98 vs deepseek 0.66，
  且后者在同一份输入上会翻面。`--reviewer none` 退回同模型复核。

## 命令

```bash
docker compose up -d postgres litellm     # postgres:5433 / litellm:4000
                                          # 不起的话 8 个测试 skip（3 个 PG + 5 个 LiteLLM，不是失败）
                                          # 另有 6 个 Docker 沙箱用例要的是 docker 守护进程本身，
                                          # 与这两个容器无关 —— 三样都缺就是 14 个 skip
python -m unittest discover -s tests -t . # 264 个测试。项目用 unittest，没引 pytest

python -m unittest tests.test_preemption                              # 单个文件
python -m unittest tests.test_chain.TestChain.test_rebase_cleared_the_trace  # 单个用例
python -m unittest discover -s tests -t . -v                          # 看每个用例名

python -m cowork.cli models                     # 拿各家 /v1/models 对一遍 PROVIDERS 表
python -m cowork.cli demo                       # SQLite + 本地沙箱 + 脚本后端
python -m cowork.cli demo --store pg --docker   # Postgres + Docker 沙箱
python -m cowork.cli demo --backend deepseek    # 真实模型，9 家见 cli.PROVIDERS
python -m cowork.cli demo --json                # 每行一条 JSON，末尾一份完整结果
python -m cowork.cli inspect <db.sqlite>        # 导出某个库的任务与 DecisionRecord
python -m cowork.cli composite                  # M4 复合任务：并行 + 冲突检测
python -m cowork.cli composite --reviewer none  # 退回同模型复核（默认已是 kimi 独立复核）

python -m cowork.cli plan "<一句话目标>"          # M7：拆解 + 复核，不执行（约 10-35k token）
python -m cowork.cli plan "<目标>" --run          # 一路跑到产出：拆解 → 分层 → 并行执行
python -m cowork.cli plan "<目标>" --gate cli     # 升级给人时自己在终端拍板

python -m cowork.cli bench --backend deepseek --repeat 5  # M2 跑批，约 25 分钟 / 1.6M token
python -m cowork.cli bench --tasks PROBE_AB --repeat 5    # M3 的 PROBE vs TRUST 对照
python -m cowork.cli bench-report bench_runs.jsonl        # 只出报告，不重跑

python -m cowork.cli bench-review --repeat 5              # M7 7.2 跨模型复核，约 25 分钟 / 0.2M token
python -m cowork.cli bench-review --cases complete        # 只跑负例（id / 家族名 / 缺陷形态都收）
python -m cowork.cli bench-review-report review_ab.jsonl  # 只出报告，不重跑

python -m cowork.cli bench-plan --repeat 3                # M7 7.4 拆解提示词对照，约 50 分钟 / 0.8M token
python -m cowork.cli bench-plan --goals wc --repeat 1     # 冒烟（--arms full,naive 可选一个）
python -m cowork.cli bench-plan-report plan_ab.jsonl      # 只出报告，不重跑
```

`bench` 要花真钱和 25 分钟，跑之前先用 `--tasks p1_word_count --repeat 1` 冒烟。
`--tasks` 收任务 id 或**类别**（`bench/tasks.py` 的 `default_tasks()`）：
`PASS` / `ONE_REBASE` / `MULTI_REBASE` / `ESCALATE` 是 M2 的四类，`PROBE_AB` 是 M3 的。

仓库根的 `*.jsonl` 是历次跑批的原始记录 —— `policy.py` 的参数依据全在里面，
`bench-report <文件>` 随时能重出报告，**不用重跑也不用再花钱**：

| 文件 | 是什么 |
|---|---|
| `bench_runs.jsonl` | M2 全量 75 次（四类 × 5 次），§11.6 的底稿 |
| `probe_runs.jsonl` | M3 的 PROBE vs TRUST 三 arm 各 5 次（§11.7） |
| `m5a_after.jsonl` / `m5a_regression.jsonl` | M5a **第一版**提示词的不可解侧 / 可解侧 —— 就是「无差别放弃」那次回归 |
| `m5a_v2_escalate.jsonl` / `m5a_v2_solvable.jsonl` | 重写后同口径的两侧（§11.9c，Youden J 0.60） |
| `review_ab.jsonl` | M7 7.2 的**最终**数据（§11.11），= 下面两个 v2 文件合并 |
| `review_ab_positives_v2.jsonl` / `review_ab_negatives_v2.jsonl` | 返工后的正例 90 次 / 负例 30 次 |
| `review_ab_v1.jsonl` | 用例表返工**前**的 120 次。别删 —— 它是「负例必须真的完整」那条坑的证据 |
| `plan_ab.jsonl` | M7 7.4 的 37 次拆解（§11.13）：提示词两臂 × 6 目标，`max_regenerate` 的依据 |

M5a 那四个文件是「改提示词必须两侧都测」的现成对照组，改停止判据时拿它做基线。
`review_ab*` 同理，改复核提示词或换复核模型时拿它做基线；
`plan_ab.jsonl` 是改拆解提示词或 `max_regenerate` 时的基线。

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
  （超中断上限、**信号指纹重复**、scope 越界、**任何 ABANDON**、顶层 MODIFY_TASK、
  不可逆命令…），命中就交 `HumanGate`；没命中才看 LLM 自评的 `complexity_score`。
- **架构师记得自己试过什么**（M5a）。`Architect._history` 按任务存 (指纹, 动作, 理由)，
  同时喂两处：确定性的「决策无效」判据，和 `decide_interrupt(history=...)` 的提示词。
  没有它的话架构师每次都在「第一次见到这个问题」的状态下决策。
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
  开跑前还有一道拆解复核（M5b，见上面 M7 那节）：没给 `root_goal` 就只做结构检查，
  跳过要花钱的语义那半。
- **上面这条链的入口现在还可以再往前一步**（M7）：`Architect.plan(root_goal, template)`
  跑「生成 → 复核 → 重生成 ≤ `max_regenerate` → 升级给人」，产出的 specs 直接交给
  `Scheduler`。终局同样是三种、同样都不是异常：
  `ACCEPTED` / `AWAITING_HUMAN`（含模型调不动、没有人的入口）/ `REJECTED`。
  `cli plan --run` 就是把这两段接起来。

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
- **改提示词必须两侧都测**。M5a 第一版把 `ABANDON` 的判据写宽了，不可解任务上
  主动放弃 12%→96% 看着是大胜，可解任务完成率同时从 81% 塌到 56%、
  `MULTI_REBASE` 归零 —— 它不是判别力变强，是**无差别放弃**。
  **提示词只能调偏置；要判别力得让它先分辨证据的性质**（重写后 Youden J 0.12→0.60）。
  改任何影响「继续还是停」的东西，正例反例都要跑。
- **确定性护栏是兜底不是主力**。停滞判据在「无差别放弃」那版只命中 1 次，
  在平衡版命中 17 次 —— 提示词走极端时护栏根本没机会触发。别拿护栏的命中数
  当改动生效的证据。
- **对照实验的负例最容易是自己写错**。M7 7.2 第一轮两个 arm 在同一个「完整」拆解上
  10/10 全报缺口，看着是假阳性爆表；读原文发现原始目标里的「一页」根本没有验收标准
  管它 —— **复核者是对的，用例表是错的**。写负例的方法因此定死：把目标里的限定词
  逐个划出来，每个都要指得到一条判据。反过来也提醒：**返工用例表之后，共用子任务的
  正例也要重跑**，两轮数据不能混着算。
- **但也不能改到 FPR 归零**。v2 里还剩一条争议性的报缺口，我们决定不修 ——
  继续改用例直到指标好看，就是拿模型输出拟合测试集，测出来的只是改了几轮。
- **模型的空回复不能原样回灌进修复轮**。`openai_compat` 在 JSON 不合规时会带着原文
  再问一轮，而空串做 assistant 消息会被端点判 400（`must not be empty`），
  一次可恢复的解析失败就此升级成硬失败。120 次调用栽了 2 次。
- **子进程输出不能按系统编码解码**。中文 Windows 上 `text=True` 走 GBK，被测程序吐一个
  非 GBK 字节，解码在 `subprocess` 的读取线程里炸掉、`proc.stdout` 变 `None`，
  一直到 `loop.py` 拼证据时才以 `TypeError` 现形。现在固定
  `encoding="utf-8", errors="replace"`：证据宁可花几个字符，不能丢整条链路。
- **同一份输入上会翻面的模型是弱证据**。`deepseek-reasoner` 在一个一字未改的用例上
  两轮报出率 4/5 → 1/5，kimi 是 9/9。选复核模型时稳定性和 J 值一样重要 ——
  复核结果要驱动「重生成还是升级给人」，它自己抖动等于把噪声接进控制流。
- **推理型模型的 thinking 计在 `max_tokens` 里，而且方差极大**。同一个拆解请求
  `deepseek-v4-flash` 的 reasoning 落在 2093~12000，有一次把 12000 全烧在思考上、
  正文 0 字符。三件事都要做：额度给够（拆解 16000）、**把截断单独认出来**
  （截断的 JSON 报出来是「不是合法 JSON」，照着查会查错方向）、
  **截断后原样重掷而不是带残文修复**（残文回灌只会让它接着写半截 JSON）。
- **提示词的拼装顺序就是缓存命中率**，而且它是条沉默的不变量：静态（角色提示词 +
  输出约束 + schema）在前、可变（目标 / 上下文 / 执行记录）在后。把 schema 挪进
  user、或在 system 里插个任务 id，**功能测试全绿、命中率直接归零，账单下个月才
  告诉你**。现在有 `test_openai_compat.TestPromptCaching` 钉着。实测 deepseek 74%。
- **缓存字段各家名字不一样，只认一个就会读成 0**：OpenAI 系
  `prompt_tokens_details.cached_tokens`、DeepSeek 另给 `prompt_cache_hit_tokens`、
  Moonshot 还有顶层 `cached_tokens` 且**首次调用 details 是 null**、
  Anthropic 的 `cache_read_input_tokens` **不含在 input_tokens 里、要加回去**。
  另外「这家不报」和「没命中」在账面上一样但结论相反，要分开记。
- **Anthropic 的缓存是显式的**，不打 `cache_control` 断点一次都不命中 ——
  和 OpenAI 系「够长就自动缓存」不是一回事，删掉断点不会有任何测试变红。
- **`PROVIDERS` 表会无声地过期**：模型下线时端点还在、key 还有效，只有那个 id 没了。
  别读文档判断，跑 `python -m cowork.cli models`。表里 `verified` 记的是
  「本机用真 key 打通过」—— 没打通过不等于错，等于没验证，两者不能混。
- **改一处 token 额度时，问一句同一条链上还有谁的输入变长了**。M7 把拆解调用提到
  16000，复核调用留在 4096 —— 而复核要读完整份拆解再推理，实测被吃满、正文 0 字符。
- **两侧的失败要走同一条路**。`plan()` 第一版只接住生成者的 `ModelError`，
  复核者失败就抛穿整个循环 —— 手上明明有拆解，却因为「没人复核得了」而崩掉。
  凡是「A 失败有兜底」的地方，都要问 B 失败走哪儿。
- **空产出不能被「同意」**。生成者调不动模型时手上是空列表，而 `AutoApproveGate`
  对什么都点头 —— 于是「拆解失败」被记成「ACCEPTED，0 个子任务」。
  **这个洞在脚本后端上永远暴露不了**，因为脚本后端不会调用失败。
  凡是「网关点头就通过」的地方，都要先问一句「手上真的有东西可以通过吗」。
- **模型会用「一人一个子目录」来满足 scope 不相交**。三个子任务分别产出
  `subtask1/`、`subtask2/`、`subtask3/`，而第三个 import 前两个 —— scope 确实不相交，
  代价是运行时 import 不到。结构层和语义层都看不见它：那是第三个问题
  「合起来能不能跑」。`plan.isolated_dependency` 只堵了这一种形态。

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
actions.py      Subagent 每 step 只能产出这三种：ToolCall / Finish / SoftSignalAction
signals.py      §3 信号协议 + task_class → 硬信号覆盖面
policy.py       §9 待验证参数
escalation.py   §7.2 确定性升级下限 —— 执行层与拆解层（M7 7.4）共用这一个模块
resume.py       §6 RESUME / REBASE / RESTART
orchestrator.py §5 状态机 + PROBE 分段（M3）
plan.py         §12 M4 拓扑分层 / 可分解性 / 静态 scope 冲突 + M5b 结构性复核
                + M7 isolated_dependency —— 全确定性
scheduler.py    §12 M4 并行调度 + 产出层冲突检测 + 仲裁
config.py       .env 加载（环境变量优先，空值=未设置）+ redact，不引 python-dotenv
demo*.py        M1 单任务 / M4 复合任务的验证场景 —— 「隐藏要求」写在这里
runtime/        确定性层：bus / sandbox / detectors / loop —— 这里不许出现 LLM 调用
agent/          architect（唯一写入决策点：中断决策 + 拆解生成 plan()/decompose()；
                reviewer_backend = 无写权的复核者）/ subagent（薄绑定层）
llm/            __init__ 是后端协议本身：Backend Protocol + ArchitectVerdict /
                Triage / CacheStats，
                **给模型加一种能力从这里改**；scripted（确定性测试用）/
                anthropic_backend / openai_compat / errors
store/          sqlite（默认）/ postgres（正式）
bench/          §12 M2/M3/M5 实测 —— 只包装不改被测对象，不参与生产链路
                review_ab.py 是 M7 7.2：12 个带标准答案的拆解 + TPR/FPR/J
                plan_ab.py  是 M7 7.4：提示词两臂 + 生成-复核循环的指标
```
