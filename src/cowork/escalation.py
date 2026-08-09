"""升级边界（§7）。

§7.2 的要点：纯靠 LLM 自评复杂度有盲区——模型给低分的场合，
恰恰可能是它没意识到问题严重性的场合。所以下面这组规则**不经 LLM 判断**，
与 complexity_score 是「或」的关系，任一命中即升级，LLM 无权覆盖。
"""

from __future__ import annotations

from .llm import ArchitectVerdict
from .policy import Policy
from .signals import SignalType
from .types import Signal, TaskSpec, TaskState


def deterministic_escalation(
    policy: Policy,
    spec: TaskSpec,
    state: TaskState,
    signals: list[Signal],
    verdict: ArchitectVerdict | None = None,
) -> str | None:
    """命中任一条则返回升级理由；否则返回 None。"""

    # 1. 决策涉及不可逆操作：影响面不可回滚，与 LLM 的自信程度无关
    marker = _irreversible_marker(policy, spec, verdict)
    if marker:
        return f"决策涉及不可逆操作（命中标记 {marker!r}）"

    # 2. 反复中断说明 LLM 没找到根因，再让它试是浪费
    if state.interrupt_count >= policy.max_interrupts:
        return (
            f"同一 task 的 interrupt_count 已达 {state.interrupt_count}"
            f"（阈值 {policy.max_interrupts}）"
        )

    # 3. 触及用户原始意图
    if (
        policy.escalate_on_toplevel_modify
        and spec.parent_id is None
        and verdict is not None
        and verdict.action in ("MODIFY_TASK", "ABANDON")
    ):
        return f"要对 parent_id 为空的顶层任务执行 {verdict.action}"

    # 4. 已越界，需人确认边界是否该扩
    if policy.escalate_on_scope_violation and any(
        s.type is SignalType.SCOPE_VIOLATION for s in signals
    ):
        return "触发信号包含 SCOPE_VIOLATION"

    # 5. 成本失控
    if spec.token_budget and state.tokens_used > spec.token_budget * policy.budget_escalation_ratio:
        pct = state.tokens_used / spec.token_budget
        return f"累计 token 消耗达预算的 {pct:.0%}（阈值 {policy.budget_escalation_ratio:.0%}）"

    return None


def _irreversible_marker(
    policy: Policy, spec: TaskSpec, verdict: ArchitectVerdict | None
) -> str | None:
    haystack: list[str] = list(spec.tools)
    for crit in spec.acceptance:
        if crit.command:
            haystack.extend(crit.command)
    if verdict:
        for crit in verdict.spec_changes.get("added_criteria", []):
            haystack.extend(crit.get("command", []) or [])
    for token in haystack:
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in policy.irreversible_markers:
            return base
    return None


def should_escalate(
    policy: Policy,
    spec: TaskSpec,
    state: TaskState,
    signals: list[Signal],
    verdict: ArchitectVerdict,
) -> str | None:
    """确定性下限 OR LLM 自评超阈值。"""
    hard = deterministic_escalation(policy, spec, state, signals, verdict)
    if hard:
        return hard
    if verdict.complexity_score >= policy.complexity_threshold:
        return (
            f"LLM 自评 complexity_score={verdict.complexity_score:.2f}"
            f" >= 阈值 {policy.complexity_threshold}"
        )
    return None
