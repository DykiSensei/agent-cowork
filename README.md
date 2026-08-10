# 多 Agent 协作系统 —— 原型

对应开发文档 §12 路线图。

```
M0 ✅ 核心链路验证    L0 硬信号 → 中断 → REBASE → 恢复
M1 ✅ 真实环境收口    Postgres / Docker 沙箱 / 真实模型 / virtual key 预算强制
M2 ✅ 参数实测        15 个任务 × 5 次 = 75 次真实运行，policy.py 六个值有了依据
M3 ✅ PROBE 模式      成本 1.45x 已量化；收益（27 次探查 0 次命中）未标定
M4 ✅ 并行与冲突检测  4 子任务 / 2 种 task_class / 并行度 2，真实模型跑通
M5a ✅ 停止判断       判别力 Youden J 0.12 → 0.60（第一版是回归，见下）
M5b ✅ 拆解复核       验收标准反推 10/10 命中。**但拆解生成侧从未实现**
M7 ✅ 拆解三角色      生成者 / 复核者 / 人。跨模型复核 J 0.66 → 0.98；
                     5 个自然语言目标全部拆出可执行子任务集并跑到产出
M6 ✅ 群聊界面层      前端 ui/（React + TS，简洁/专业双模式 + 设置页）+
                     服务层 src/cowork/server/（FastAPI + SSE + restore 路径）
```

**M0–M7 全部完成。** 一句话目标进去，拆解 → 复核 → 分层并行执行 → 中断 →
人裁决 → 从 checkpoint 恢复，全程可以在界面上看着跑。

在读下去之前，先看一眼[这个原型不适合做什么](#这个原型不适合做什么) ——
它是**给一个人在本机用的研究原型**，不是可以对外提供服务的东西。

---

## 跑起来

```bash
docker compose up -d postgres litellm
python -m unittest discover -s tests -t .
```

377 个测试。三样基础设施都不起时，依赖它们的 14 个会 skip（3 个 Postgres +
5 个 LiteLLM + 6 个 Docker 沙箱），其余照常跑；打真实供应商的用例需要
`DEEPSEEK_API_KEY` 或 `MOONSHOT_API_KEY`（`.env` 里配好即可，测试会自动读）。

```bash
python -m cowork.cli demo                      # SQLite + 本地沙箱 + 脚本后端
python -m cowork.cli demo --store pg --docker  # Postgres + Docker 沙箱
python -m cowork.cli demo --backend deepseek   # 真实模型（需要 key，见下）

python -m cowork.cli composite --backend deepseek          # M4 复合任务：并行 + 冲突检测

python -m cowork.cli plan "把 CSV 转成带图表的周报" --run    # M7：一句话 → 拆解 → 并行跑到产出

python -m cowork.cli bench --backend deepseek --repeat 5   # M2 参数实测跑批
python -m cowork.cli bench-report bench_runs.jsonl         # 出参数结论
```

（未安装时先 `pip install -e .`，或 `set PYTHONPATH=src`。）

带界面跑：

```bash
pip install -e .[server]
cd ui && npm install && npm run build && cd ..
python -m cowork.cli serve      # http://127.0.0.1:8000
```

`serve` **只绑 loopback，绑别的地址会被直接拒绝** —— 理由见
[这个原型不适合做什么](#这个原型不适合做什么)。只想看界面不想起后端的话，
`cd ui && npm run dev` 自带 mock API（数据是真实运行导出的，见 `ui/README.md`）。

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
| 执行层中心化，Subagent 只与架构师通信 | 没有 Subagent 间通信的 API 面；并行度加在 `scheduler.py`，上游产出经调度器注入 | `test_scheduler` |
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

DeepSeek（v4 起三个角色统一 `deepseek-v4-flash`）和
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

## M2 实测结论

15 个任务 × 5 次 = 75 次 DeepSeek 真实运行，1.62M token。工具在 `src/cowork/bench/`，
原始记录 `bench_runs.jsonl`，完整分析见开发文档 §11.6。

### 改了四个参数

| 参数 | 原值 | 现值 | 依据 |
|---|---|---|---|
| `complexity_threshold` | 0.6 | 0.4 | ROC 最佳 Youden 点（TPR 0.66 / FPR 0.35）；原值只有 TPR 0.38 |
| `max_rebase` | 3 | 2 | 完成率 REBASE 2 次 41%、3 次 33%、**4 次 0%** |
| `budget_escalation_ratio` | 0.8 | 0.6 | 0.8 越线后中位只剩 0 token 就到终局，等于事后通知 |
| ~~`step_soft_deadline_s`~~ | 60 | 已删除 | 定过 30s（step 耗时 p99 5.9s / max 10.8s，n=651），但没有任何代码读它 —— v0.20 删掉参数，测量保留 |

`max_interrupts=3` 是唯一被数据支持保留的：条件成功率 ≥3 次 18%、≥4 次 7%、≥5 次 0%。

### 三条比参数更重要的结论

**1. LLM 自评复杂度判别力很弱（AUC 0.672）。**
90 条「该升级给人」的决策里，**63 条是被 §7.2 的确定性规则拦下的，不是被自评分数**。
最说明问题的是 `e1_silent_failure` —— 验收脚本静默失败、架构师手上零证据，
它的自评分数中位只有 0.3。§7.2 那句「模型给低分的场合恰恰可能是它没意识到问题
严重性的场合」被数据支持了。

**2. 架构师占掉总 token 的 51.3%。**
`decide_interrupt` 176 次调用、中位 3536 token。每次中断周期的边际成本约 7k token
（0 次中断 3.1k → 1 次 10.5k → 2 次 16.8k → 3 次 27.1k）。系统里最贵、最有权、
且唯一无人复核的组件是同一个 —— 这让 M5 更紧迫。

**3. 实测的一半价值是告诉你哪些参数不该存在。**
`soft_queue_threshold` / `soft_interval_s` 在当前调用路径上是死的（**v0.21 已删**）：
`Architect.should_consume_soft()` 没有任何调用方，而且软信号极稀疏
（75 次运行 20 条，队列深度最大 2，阈值 5 永远达不到）。`step_soft_deadline_s`
同样没有代码读它。结论是「接上或删掉」，不是编一个数 ——
**v0.20 把 `step_soft_deadline_s` 删了**（测量保留在 `analyze.interrupt_latency()`）。

顺带证伪了风险 #1 的前提：checkpoint 写入耗时中位 **0.2ms**，占 step 总耗时的
**0.009%** —— step 粒度完全不需要为 checkpoint 开销让步。

### 跑批期间修掉的两个缺陷

- **探测性 `read_file` 被当成硬信号**。Subagent 第一步几乎总是探一下产出文件在不在，
  「不存在」返回 `ok=False` 就被判成 `TOOL_FAILURE` 抢占，每个任务白烧一轮架构师决策。
  修完 `PASS` 类任务从「2 次中断 / 12.2k token / 78s」降到「0 次中断 / 2.5k token / 10s」。
- **动作解析失败抛异常穿透整个 run**。`ACTION_SCHEMA` 用空串表示「不适用」，于是
  `kind=tool_call` + `tool=""` 能过校验再往下炸，75 次运行死了 3 次。现在改抛
  `ModelCallFailed`，走硬信号通道 —— 和模型调用失败同一条路。

### 任务集设计上的教训

隐藏约定全部选**与通行理解相反**的项目约定（保留最后一次出现、不足一块就丢弃、
`n<=0` 返回原串…），推理再强也推不出来。验收脚本的用例表存成压缩 blob ——
`read_file` 不受 scope 限制，明文写用例等于把答案发给 Subagent。

但仍然漏了一条：**验收命令一旦对 Subagent 可执行，失败信息本身就把答案说出来了**。
追踪显示 Subagent 自己跑 `python verify.py`，看到
`FAIL: is_palindrome(*['']) -> True, expected False` 就直接改对了，
架构师被叫来只说了句「继续」。所以任务集验证的是「失败信号驱动收敛」，
不是「架构师改规格驱动收敛」——这是 M1.3 那条教训的新形态。

### 给 M4 加的两项

- **`list_files` 工具**：缺它导致约三成运行触发假阳性 `SCOPE_VIOLATION`
  （模型想探查工作区只能去调 `ls`，撞 `allowed_binaries`）。75 次运行 23 次。
- **区分「Subagent 主动跑验收命令失败」与「工具坏了」**：当前 `run` 非零即抢占，
  占全部硬信号的 50%，把正常的自测-修复循环切成反复打断架构师。

---

## M3 实测结论（PROBE）

同一个写作任务三个 arm，只差 `silence_policy` 与探查间隔，各跑 5 次
（`probe_runs.jsonl`，完整分析见开发文档 §11.7）。

### 表面数字是错的

| arm | 探查次数(中位) | token 中位 | 表面溢价 |
|---|---|---|---|
| `g0_trust` | 0 | 8024 | 1.0x |
| `g1_probe_20s` | 1 | 27259 | 3.40x |
| `g2_probe_5s` | 4 | 29080 | 3.62x |

单次探查中位只有 **1176 token**。1 次探查换 +19k token 的差额，算术上就说不通 ——
差额几乎全部来自 `decide_interrupt`：三个 arm 抽到的中断次数不同（中位 1/3/4），
而每次中断约 7k token。

**控制住中断次数后（只看零中断的运行），净溢价是 1.45x（20s）/ 1.64x（5s）。**
开发文档 §12 M3 那条「溢价 >3x 就回头重新考虑 §3.2.1」的判断点因此**不该触发** ——
它差点被一个没控变量的中位数比较误触发。这是「单次运行是噪声」的推广形态：
**跨组比中位数时，高方差项没控住的话，中位数比较本身就是噪声**。

### 比成本更值得记的：PROBE 一次都没抓到东西

**27 次探查，0 次判跑偏。** 花掉 12–16% 的 token，收益在这批数据上是零。
两种可能还没被区分开：任务本身没漂移，或者探查提示词太宽松
（`PROBE_SYSTEM` 明确写了「拿不准就判在轨」，因为误报的代价是白打断一次）。

**PROBE 的成本已知、收益未知。** `default_probe_interval_s=20.0` 只有成本侧依据，
保守取长不取短。

### 实现期踩的坑

**探查分段会把资源上限清零。** 每次探查后重新进 `loop.run`，`max_steps` 和
`deadline_s` 的计数跟着清零 —— PROBE 任务因此变成没有步数上限也没有超时。
而 §3.2.1 说 GENERATIVE 只剩 `TIMEOUT` / `STEP_LIMIT` / `BUDGET_EXCEEDED` 三条硬信号，
这个 bug 一次干掉两条。**给循环加「让出控制权」的能力时，所有以循环为计量单位的
东西都要跟着走。**

---

## M4 实测结论（并行与冲突检测）

4 个子任务、2 种 `task_class`、并行度 2 的复合任务在 DeepSeek 上跑通：
全部 `COMPLETED`，25.9s / 25.7k token / 零中断。

```
层1  t1_parse (CODE)  ‖  t2_format (CODE)     scope 不相交，真并行
层2  t3_report (CODE)                          depends_on 两者，拿到只读上下文
层3  t4_check (TOOL_CALL)                      跑全量校验
```

**并行度加在调度层，不是通信层。** 没有新增任何「任务 ↔ 任务」的 API 面 ——
下游拿到上游成果的唯一途径是调度器把 artifact 作为只读上下文注入。

**可分解性是算出来的，不是模型说了算**（`plan.py`，全确定性无 LLM）：

- **同层 scope 有交集 → 整层串行化。** 不做「求最大独立集」的部分并行优化：
  收益不确定，而错了的代价是**静默覆盖** —— 并行写同一个文件不会报错，
  先写的那份产出就那么没了。
- **没有任何一层能并行 → 标记 `fan_out`。** 顺序依赖强的任务多 agent 最差 −70%，
  这种拆解应该退化为单 agent。

**新增一条 L0 硬信号 `CONFLICT_DETECTED`**，与 L1 的 `CONFLICT_SUSPECTED` 是两回事：
后者是 Subagent「怀疑」，前者是调度器确定性观测到两个任务写了同一份产出。

「同层」这个限定是本质的：**跨层写同一个文件是有序交接**，判成冲突会让正常拆解
跑不动。于是运行期还能撞上的冲突只剩一种 —— 架构师在运行中用 `MODIFY_TASK`
改宽了 scope，把两个并行任务撞到一起，正是静态检查看不到的那种。

**仲裁不新开决策通道**：冲突归属给后写的那个任务，走既有的 `Architect.decide()`。
为冲突单开一套裁决逻辑，等于承认「架构师是唯一写入决策点」不成立。

**并行暴露的存储层缺陷**：`sqlite3` 连接不是线程安全的，已改为
`check_same_thread=False` + 方法级 `RLock`。锁必须覆盖到 `fetch` ——
只锁 `execute` 的话游标会在锁外回头碰连接。

---

## M5a 实测结论（架构师的停止判断）

M2 归因定的方向：架构师的失效形态不是「规格拆错了」，是**「不知道该停」**。
三条改动：确定性的「决策无效」判据（零成本）、把决策历史喂给它、给 `ABANDON`
写明判据。

### 第一版是一次真实的回归，只有对照组发现了它

在不可解任务上，第一版效果惊人：主动 `ABANDON` **12% → 96%**，token 中位
39.4k → 15.3k。**只看这一组数据，我会把它当成改进提交。**

可解任务的对照组说的是另一回事：

| 版本 | 可解任务完成率 | 误放弃 | `MULTI_REBASE` |
|---|---|---|---|
| v0 基线 | 39/48 = **81%** | 0 | 5/13 |
| v1 第一版 | 28/50 = **56%** | 22 | **0/15** |

v1 的 50 次运行里架构师只做出过一种决策：**`ABANDON` 22 条，`MODIFY_TASK` 归零**。
所谓 12%→96% 不是判别力变强，是**无差别放弃** —— 偏置被推到了另一端。

### 重写后：判别力真的提升了

第二版按「先判断证据的性质，再选动作」重写：证据具体且指向规格缺口 →
`MODIFY_TASK`；只有「继续下去不可能成功」**且**「改 TaskSpec 也解决不了」才放弃。

把「该不该放弃」当二分类看（n≈25 不可解 / n≈50 可解）：

| 版本 | TPR（该弃则弃） | FPR（误弃） | Youden J |
|---|---|---|---|
| v0 基线 | 0.12 | 0.00 | 0.12 |
| v1 第一版 | 0.96 | 0.44 | 0.52 |
| **v2 现版** | **0.80** | **0.20** | **0.60** |

v2 在可解任务上完成率 80%（基线 81%，`MULTI_REBASE` 反而 5/13 → 7/15），
在不可解任务上中断中位 5 → 2、token 中位 38.5k → 33.7k。

### 确定性护栏是兜底，不是主力

停滞判据（连续两次中断的信号指纹相同 → 强制升级）的命中次数：
v0 **0** → v1 **1** → v2 **17**。

v1 里它几乎不触发，因为运行在第一次中断就结束了。**只有当提示词不再无差别放弃，
这条护栏才有事可做。** 这修正了原先「确定性规则是主力」的预期。

误放弃的代价另有一条结构性规则兜住：`policy.escalate_on_abandon` 让**任何
`ABANDON` 都升级给人**（放弃对该任务不可逆，按 §7.2 第 1 条同理）。
它把误放弃的后果从「任务没了」降级成「打扰人一次」——
但在 `AutoApproveGate` 下实测看不出效果，依据是结构性的，不是实测的。

---

## M5b 实测结论（拆解复核）

**先说一个前提：架构师从来没有真的拆解过任务。** `Orchestrator` 拿到的是现成的
`TaskSpec`，`demo_composite.py` 那 4 个子任务是手写的。§2.3 把「任务拆解」列为
架构师职责，**生成侧从未实现**。所以 M5b 做的是复核侧。

两层，可信度不同，因此在数据结构上也是分开的两个字段：

| 层 | 查什么 | 成本 | 会不会漏判自己 |
|---|---|---|---|
| `plan.deterministic_review()` | 结构：依赖悬空、有环、无 scope、拆了等于没拆 | 零 | 不会 |
| `Backend.review_decomposition()` | 语义：满足这些验收标准是否等于完成原始目标 | 一次调用 | 会 |

先结构后语义 —— 结构就是坏的时候，语义复核既没意义也不该为它花 token。

方法选的是**验收标准反推**：正向问「这个拆解好不好」只会得到复述，
反推问「按这些标准验收完还缺什么」才逼出遗漏。

实测（M4 复合场景，各 5 次）：

| 输入 | 期望 | 结果 |
|---|---|---|
| 完整 4 个子任务 | `sufficient=true` | **5/5 正确，零假阳性** |
| 摘掉 `t2_format` | `sufficient=false` | **5/5 正确**，每次都点名缺格式化 |

模型原话：「没有任何子任务负责实现格式化组件（如 `formatter.format_row`），
原始目标中的『格式化』部分未被覆盖」。

**风险 #3 只被削弱，没有被消除**：复核者与拆解者是同一个模型（同一个脑子换个
问法再想一遍，不是独立复核）；只测了一种缺陷形态（整个子任务缺失）；
生成侧不存在，所以「架构师自己拆出来的质量如何」仍是空白。

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
    __init__.py         Backend 协议 + ArchitectVerdict / SubtaskDraft / CacheStats
    scripted.py         确定性后端
    anthropic_backend.py 真实后端（提示词与 schema 都在这里，openai_compat 复用）
    openai_compat.py    OpenAI 方言：其余 8 家都走它
    errors.py           provider 错误 → L0 硬信号
  store/
    sqlite.py           零依赖，默认
    postgres.py         §10.2 正式选型
  plan.py               §12 M4 拓扑分层 / 可分解性 / 静态冲突（全确定性）
  scheduler.py          §12 M4 并行调度 + 产出层冲突检测 + 仲裁
  views.py              M6 投影层：Store → 界面层契约的形状，无业务逻辑
  server/               M6 服务层（pip install -e .[server]）
    app.py              FastAPI 路由，只做「调 runner/views + 序列化」
    runner.py           线程编排 + plan 注册表 + restore 路径
    gate.py             ChatGate：摆出问题、立即返回，答复走 HTTP
    tap.py              写入处发事件 → SSE（先落库后广播）
    bind.py             绑定地址准入检查 —— 非回环直接拒
    settings_io.py      设置页写 .env（键名与换行都要校验，见下）
  demo_composite.py     M4 出口场景：4 子任务 / 2 种 task_class / 并行度 2
  bench/                实测工具（不参与生产链路）
    tasks.py            M2 的 15 个任务 + M3 的 PROBE 对照 arm
    runner.py           跑批 + 仪表化，只包装不改被测对象
    analyze.py          从记录推参数：ROC / 分布 / 条件成功率 / PROBE 溢价
    review_ab.py        M7 7.2 跨模型复核对照：12 个带标准答案的拆解 + TPR/FPR/J
    plan_ab.py          M7 7.4 拆解提示词对照 + 生成-复核循环指标
```

界面层在仓库根的 `ui/`（React 18 + TS + Vite，双模式 + 设置页，自带 mock API），
细节见 `ui/README.md`。

根目录还有两份文档：`多Agent协作系统-开发文档.md`（主线，设计与全部实测记录）、
`M6-界面层接口.md`（给界面层那一侧看的接口约定）。

---

## 接真实模型

两个后端，同一个 `Backend` 协议：

| 后端 | 方言 | 供应商 | CLI |
|---|---|---|---|
| `llm/anthropic_backend.py` | Anthropic Messages | Claude 全系 | `--backend anthropic` |
| `llm/openai_compat.py` | OpenAI Chat Completions | 其余全部 | `--backend deepseek` / `kimi` / `openai` / `gemini` / `qwen` / `zhipu` / `xai` / `doubao` / `litellm` |

九家的端点、key 变量名和默认模型都在 `cli.PROVIDERS` 一张表里，加一家只改那张表。

**那张表会无声地过期** —— 供应商下线一个模型 id 时，端点还在、key 还有效，
只有那个 id 没了。所以填完 key 先跑一遍自检，别读文档猜：

```bash
python -m cowork.cli models          # 逐行对 GET /v1/models
python -m cowork.cli models deepseek # 只看一家
```

它区分「对不上」（表错了）和「跳过」（缺 key，这次没验证到）—— 后者不代表配置有问题。

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

默认分工见 `cli.PROVIDERS`，用 `COWORK_ARCHITECT_MODEL` / `COWORK_SUBAGENT_MODEL` /
`COWORK_TRIAGE_MODEL` 覆盖。DeepSeek 从 v4 起只暴露 `deepseek-v4-flash` / `-pro`，
三个角色现在统一 flash —— 想让架构师回到推理档就设 `COWORK_ARCHITECT_MODEL=deepseek-v4-pro`。

### 经 LiteLLM（要 virtual key 的预算强制时）

```bash
DEEPSEEK_API_KEY=sk-... docker compose up -d litellm

curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-cowork-master" -H "Content-Type: application/json" \
  -d '{"models":["deepseek-v4-flash","deepseek-v4-pro"],"max_budget":5.0,"key_alias":"task-xxx"}'

COWORK_LLM_BASE_URL=http://localhost:4000/v1 \
COWORK_LLM_API_KEY=sk-<上面返回的 virtual key> \
python -m cowork.cli demo --backend litellm
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

## 这个原型不适合做什么

按「能不能交给别人跑真实活儿」这个标准，下面几条是**设计层面的**，不是待办：

**不要把它服务化给多个人用。** 没有认证、没有多用户概念 —— `HumanGate` 不知道
「哪个人」在回答。而设置页能读写 `.env` 里的各家 API key 和
`COWORK_LLM_BASE_URL`。这三件事叠在同一个 HTTP 面上，暴露到回环之外就等于
交出账号：任何能访问那个端口的人都能取走 key、把所有请求改道、用你的额度起任务。
所以 `serve` 绑非回环地址会**直接拒绝启动**（要过就得显式 `--i-know-its-exposed`，
且仍然警告）。真要多人用，需要的是认证 + 权限模型，不是一个开关。

**不要指望它替你控制成本。** 应用层只有 `budget_escalation_ratio` 这类软限制；
真正的硬强制来自 LiteLLM 的 virtual key，而不起 LiteLLM 直连供应商时那层不存在。
架构师本身是系统里最贵的组件（M2 实测占总 token 的 51.3%，每次介入约 7k）。
一次 `plan --run` 打真实模型是 10–35k token 起步，跑批是 1.6M。

**架构师仍然是唯一的写入决策点，执行期对它的复核默认没开。** M7 解了拆解那半；
M8 把复核接到了 `decide()` 的写入侧（`Architect(review_writes=True)` 启用），
实测 J 0.886（kimi 复核）/ FPR 0.000。它接得住「把失败输入从目标里摘出去」
和「扩 scope 去改考题」，**接不住「答非所问的改动」** —— 不过那一种下一轮会被
「指纹重复」的确定性判据接住，是三种漏报里代价最低的（§11.19）。

这块暴露面现在是量过的：M2 的 176 条裁决里 61% 已被确定性规则送到人面前，
真实缺口是**改了 TaskSpec 且无人过目的 34 条（19%）**。其中最危险的形态是
**目标被改松** —— 把失败的那种输入从 goal 里摘出去，任务就「成功」了，
没有任何后续信号会暴露它。MAST 数据里 42% 的失败来自规格不清，就在这一层。

**服务层是单进程的，plan 注册表在内存里。** 服务重启会丢还没派发的拆解
（已派发的任务不受影响，它们在库里）。

---

## 还没做

| 项 | 状态 |
|---|---|
| **写入侧复核的默认开关** | M8 出口 5。实测三轮 165 次调用（§11.19）：**kimi J 0.886 / deepseek 0.829，FPR 两边都是 0/20**。两个模型的盲区**互补**，选型按漏报代价定（kimi 漏的那种下一轮会被「指纹重复」的确定性判据接住，deepseek 漏的两种没兜底）——**结论用 kimi**。数据支持默认开，保留意见是负例构造偏易、n=20；`review_writes` 目前仍默认关闭 |
| 「拆出来的东西合起来能不能跑」 | 风险 #16：结构层查交集与环、语义层查覆盖，都不问这个。`isolated_dependency` 只堵了一种形态（§11.12） |
| 拆解质量没有无偏度量 | 风险 #18：「复核一轮放行率」会被拆解粒度带偏，要真比质量得看派发执行后的产出（§11.13） |
| 7 家供应商未验证 | 只有 `deepseek` / `kimi` 在本机用真 key 打通过（`PROVIDERS` 表里 `verified=True` 记的就是这件事）。`openai` / `gemini` / `qwen` / `zhipu` / `xai` / `doubao` / `anthropic` 的预设是照文档抄的 —— **没验证不等于错，等于没验证**。有 key 就跑 `cowork.cli models` 对一遍（§11.14）。注意模型下线时端点还在、key 还有效，只有那个 id 没了，所以别读文档判断 |
| 界面上没接的三处 | 选中 `MODIFY_TASK` 时的 spec 编辑区、子任务 thread 跳转、复合任务介入时路由到哪个子任务（`ProStream.tsx` 里都标着） |
| ~~M2 的遗留~~ | **已了结**：三个死参数全部删除（v0.21）。`step_soft_deadline_s` 无人读；`soft_queue_threshold` / `soft_interval_s` 有读者但那个方法没有调用方。三处的测量都保留在 `bench/analyze.py` 里 —— 删的是参数，不是证据 |
| PROBE 的收益 | 27 次探查 0 次命中。要标定收益，需要一个会可靠漂移的 `GENERATIVE` 任务 |
| `e3_scope_bait` 任务 | 没按设计触发 `SCOPE_VIOLATION`，需重新设计（开发文档 §11.6g） |
