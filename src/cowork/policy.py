"""可调参数集中在这里。

文档 §9 把这些标为「待验证」——集中放置是为了实测时只改一处，
而不是在代码里到处找魔数。默认值是起步猜测，不是结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Policy:
    # §7.1：LLM 自评复杂度低于此值才自己拍板
    complexity_threshold: float = 0.6

    # §7.2 确定性下限
    max_interrupts: int = 3
    budget_escalation_ratio: float = 0.8
    escalate_on_scope_violation: bool = True
    escalate_on_toplevel_modify: bool = True
    irreversible_markers: tuple[str, ...] = (
        "rm", "del", "rmdir", "deploy", "push", "publish",
        "curl", "wget", "ssh", "kubectl", "terraform", "pay",
    )

    # §3.4 软信号消费检查点
    soft_queue_threshold: int = 5
    soft_interval_s: float = 30.0

    # 风险 #5：限制单任务 REBASE 次数，避免摘要压缩累积失真
    max_rebase: int = 3

    # 单个 step 的 soft deadline（§5.1），超过则 Runtime 强制切段
    step_soft_deadline_s: float = 60.0


DEFAULT_POLICY = Policy()
