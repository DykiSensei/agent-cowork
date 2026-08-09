"""信号协议（文档 §3）。

分级不是装饰：L0 由 Runtime 确定性产生、抢占式；L1 由 Subagent 自报、入队。
这里只定义类型与 task_class 的默认覆盖面，检测逻辑在 runtime/detectors.py。
"""

from __future__ import annotations

import enum


class SignalLevel(str, enum.Enum):
    L0 = "L0"
    L1 = "L1"


class SignalSource(str, enum.Enum):
    RUNTIME = "RUNTIME"
    SUBAGENT = "SUBAGENT"
    HUMAN = "HUMAN"


class SignalType(str, enum.Enum):
    # —— L0 硬信号（§3.2）——
    TOOL_FAILURE = "TOOL_FAILURE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    TEST_FAILED = "TEST_FAILED"
    TIMEOUT = "TIMEOUT"
    STEP_LIMIT = "STEP_LIMIT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    HUMAN_INTERVENTION = "HUMAN_INTERVENTION"

    # —— L1 软信号（§3.3）——
    AMBIGUITY = "AMBIGUITY"
    ASSUMPTION_BROKEN = "ASSUMPTION_BROKEN"
    CONFLICT_SUSPECTED = "CONFLICT_SUSPECTED"
    RESOURCE_NEEDED = "RESOURCE_NEEDED"
    PROGRESS = "PROGRESS"


HARD_SIGNALS: frozenset[SignalType] = frozenset(
    {
        SignalType.TOOL_FAILURE,
        SignalType.VALIDATION_FAILED,
        SignalType.TEST_FAILED,
        SignalType.TIMEOUT,
        SignalType.STEP_LIMIT,
        SignalType.BUDGET_EXCEEDED,
        SignalType.SCOPE_VIOLATION,
        SignalType.HUMAN_INTERVENTION,
    }
)

SOFT_SIGNALS: frozenset[SignalType] = frozenset(
    {
        SignalType.AMBIGUITY,
        SignalType.ASSUMPTION_BROKEN,
        SignalType.CONFLICT_SUSPECTED,
        SignalType.RESOURCE_NEEDED,
        SignalType.PROGRESS,
    }
)

# 资源类硬信号：与 task_class 无关，任何任务都能产生
_RESOURCE_SIGNALS = frozenset(
    {SignalType.TIMEOUT, SignalType.STEP_LIMIT, SignalType.BUDGET_EXCEEDED}
)

# 人的介入视同硬信号（§2.4），任何 task_class 都必须能被抢占
_ALWAYS = frozenset({SignalType.HUMAN_INTERVENTION})


def level_of(t: SignalType) -> SignalLevel:
    return SignalLevel.L0 if t in HARD_SIGNALS else SignalLevel.L1


def default_hard_signals(task_class: str) -> frozenset[SignalType]:
    """§3.2.1：不同 task_class 能产生的硬信号差别极大。

    这个函数的存在本身就是文档里那个隐蔽失败模式的防线——
    架构师据此知道「这个任务压根产生不了内容层信号」，从而选 PROBE。
    """
    if task_class == "CODE":
        return HARD_SIGNALS
    if task_class == "TOOL_CALL":
        return frozenset(
            {
                SignalType.TOOL_FAILURE,
                SignalType.VALIDATION_FAILED,
                SignalType.SCOPE_VIOLATION,
            }
        ) | _RESOURCE_SIGNALS | _ALWAYS
    if task_class == "GENERATIVE":
        # 稀疏，几乎无内容层判据 —— 因此 silence_policy 被强制为 PROBE
        return _RESOURCE_SIGNALS | _ALWAYS
    raise ValueError(f"unknown task_class: {task_class}")
