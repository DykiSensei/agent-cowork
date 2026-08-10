"""可调参数集中在这里。

文档 §9 把这些标为「待验证」。M2 / M3 已实测（§11.6 / §11.7）：**每个值下面那行
「依据」就是里程碑出口**。M2 的六个来自 15 个任务 × 5 次 = 75 次 DeepSeek 真实运行、
1.62M token（`bench_runs.jsonl`）；PROBE 的两个来自 M3 的三 arm 对照
（`probe_runs.jsonl`）。复现方式：

    python -m cowork.cli bench --backend deepseek --repeat 5    # M2
    python -m cowork.cli bench --tasks PROBE_AB --repeat 5      # M3
    python -m cowork.cli bench-report bench_runs.jsonl

改这些值之前请先跑一遍，别凭感觉调 —— 同一 spec 跨运行的中断次数落在 0–5 之间，
单次运行说明不了任何事（§11.5d）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    # §7.1：LLM 自评复杂度低于此值才自己拍板。
    # 依据：ROC 曲线最佳 Youden 点在 0.4（TPR 0.66 / FPR 0.35），原值 0.6 只有
    # TPR 0.38。但 AUC 仅 0.672 —— 自评分数本身判别力很弱，正负类分布大幅重叠，
    # 这个阈值调到哪都不会好太多。真正在拦住风险的是下面那组确定性规则：
    # 90 条「该升级」的决策里 63 条是被它们拦下的，不是被这个分数（§11.6c）。
    complexity_threshold: float = 0.4

    # §7.2 确定性下限
    # 依据：条件成功率随中断次数塌方 —— 被中断 ≥1 次后最终完成 46%，≥2 次 32%，
    # ≥3 次 18%，≥4 次 7%，≥5 次 0%（n=75）。3 正好落在跌破 20% 的位置，
    # 这是六个参数里唯一实测支持保留原值的（§11.6c）。
    max_interrupts: int = 3

    # §7.2 决策无效：连续几次「同样的信号 + 同样的证据」就强制升级。
    # 依据：M2 归因（§12 M5）显示架构师的失效形态是「该停时不停」——
    # ESCALATE 类 25 次运行里主动 ABANDON 只有 3 次，80% 靠上限兜住，
    # 而 e1_silent_failure（架构师手上零证据）5 次运行全程没 ABANDON 过。
    # 取 2 而不是 3：同一个办法试第二次就该停了，第三次是纯浪费。
    max_identical_interrupts: int = 2

    # 依据：0.8 几乎买不到提前量 —— 越线后中位只剩 0 token 就到终局了，等于事后
    # 通知。0.6 能提前约 4.8k token 发现，而误升级（最终本来会成功的运行）
    # 从 75 次里只增加 1 次。注意这是**应用层软限制**，实测中一次都没真正触发过
    # BUDGET_EXCEEDED：运行总是先被 max_rebase / max_cycles 终止（§11.6c）。
    budget_escalation_ratio: float = 0.6

    escalate_on_scope_violation: bool = True
    escalate_on_toplevel_modify: bool = True
    # 放弃对该任务不可逆，按 §7.2 第 1 条同理该由人拍板。M5a 之后架构师更愿意
    # 放弃了（误放弃率 20%），这条把误放弃的后果从「任务没了」降级成「打扰人一次」。
    escalate_on_abandon: bool = True
    irreversible_markers: tuple[str, ...] = (
        "rm", "del", "rmdir", "deploy", "push", "publish",
        "curl", "wget", "ssh", "kubectl", "terraform", "pay",
    )

    # §3.4 软信号消费检查点
    # 依据：**这两个值目前测不出来，因为它们在调用路径上是死的**。
    # `Architect.should_consume_soft()` 没有任何调用方 —— orchestrator 在每个
    # 检查点无条件批量消费。而且软信号本身极稀疏：75 次运行里 13 次出现过，
    # 共 20 条，队列深度最大 2，阈值 5 永远达不到。分诊总成本 13 次调用 ×
    # 中位 309 token，占总量的 0.3%，不值得为它做批处理优化。
    # 结论是「接上或删掉」，不是编一个数（§11.6c）。
    soft_queue_threshold: int = 3
    soft_interval_s: float = 30.0

    # 风险 #5：限制单任务 REBASE 次数，避免摘要压缩累积失真。
    # 依据：40 次完成的运行里**意图偏离 0 次** —— 没观测到摘要压缩导致的失真，
    # 但 REBASE ≥3 次的完成样本只有 1 个，所以风险 #5 是「无样本」而非「已证伪」。
    # 真正的信号是完成率：REBASE 2 次 41%、3 次 33%、4 次 0%（n=51）。
    # 第 4 次 REBASE 没救回过任何一次运行，上限收到 2（§11.6c）。
    max_rebase: int = 2

    # §12 M7 7.4：拆解被复核驳回后最多重生成几次，超了交给人。
    # 依据（§11.13，36 次真实拆解，其中 16 次被驳回）：第 1 次重生成救回 10/16
    # （62%），第 2 次在剩下的 6 次里再救回 2（33%），跑满仍不过 4 次。
    # 第 2 次重生成的边际收益已经明显下滑但不是零 —— 收到 1 会丢掉那 2 次，
    # 所以留 2。这和 max_rebase 的取值路径一样：看的是「下一次还救不救得回来」。
    max_regenerate: int = 2

    # §3.2.1 PROBE：架构师主动索要中间产出的间隔，以及每个产出截取多少字符。
    # 依据：M3 三 arm 对照（§11.7）。单次探查中位 1176 token；20s 间隔在这个
    # 写作任务上是 1–2 次/运行、净溢价 1.45x，5s 是 2–7 次、1.64x。
    # **只有成本侧依据**：27 次探查 0 次判跑偏，收益未标定，所以保守取长不取短 ——
    # 间隔缩短买到的是「跑偏最多晚一个间隔被发现」，而这个收益还没被观测到。
    # TaskSpec.probe_interval_s 显式设了就用它的，这里只是缺省。
    default_probe_interval_s: float = 20.0
    # 实测中产出片段最长 2150 字符，这个上限没被触碰过 —— 它是长文任务的护栏，
    # 不是当前的成本驱动项。
    probe_excerpt_chars: int = 4000

    # 这里曾有 step_soft_deadline_s（单个 step 的 soft deadline，§5.1）。**已删除** ——
    # 它从来没有任何代码读它，loop.py 并未实现 soft deadline 切段，而 M2 还在为它
    # 出一节报告、给一个「建议值」。一个不生效的参数配一份实测建议，比没有这个参数更坏。
    # 中断响应延迟的实测本身仍然有效、也仍在 bench 里（`analyze.interrupt_latency()`）：
    # step 墙钟耗时 p50 1.65s / p95 3.11s / p99 5.90s / max 10.79s（n=651），
    # 抢占只在 step 边界发生，所以这组数就是延迟本身。真要做切段时从那组数起步。


DEFAULT_POLICY = Policy()
