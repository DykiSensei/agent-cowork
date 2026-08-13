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
    *,
    identical_streak: int = 1,
) -> str | None:
    """命中任一条则返回升级理由；否则返回 None。

    identical_streak：本次中断的信号指纹已经连续出现第几次（含本次）。
    由架构师算好传进来 —— 这个模块不持有状态。
    """

    # 1. 决策涉及不可逆操作：影响面不可回滚，与 LLM 的自信程度无关
    marker = _irreversible_marker(policy, spec, verdict)
    if marker:
        return f"决策涉及不可逆操作（命中标记 {marker!r}）"

    # 1b. 决策无效：同样的信号、同样的证据又来了一遍，说明上一次决策没有改变现实。
    # 这条比下面的 interrupt_count 更早也更准 —— 它区分「试了三次不同的办法」
    # 和「同一个办法试了三次」。M2 实测显示后者是架构师的主要失效形态（§11.9a）：
    # 没有证据可依时它不会停，只会在 CONTINUE / REASSIGN / MODIFY_TASK 之间轮流试。
    if identical_streak >= policy.max_identical_interrupts:
        return (
            f"连续 {identical_streak} 次中断的信号指纹完全相同"
            f"（阈值 {policy.max_identical_interrupts}），上一次决策没有改变现实"
        )

    # 2. 反复中断说明 LLM 没找到根因，再让它试是浪费
    if state.interrupt_count >= policy.max_interrupts:
        return (
            f"同一 task 的 interrupt_count 已达 {state.interrupt_count}"
            f"（阈值 {policy.max_interrupts}）"
        )

    # 2b. 放弃对这个任务而言是不可逆的 —— 按第 1 条同样的道理，它该由人拍板。
    # M5a 实测的代价面：让架构师更愿意放弃之后，可解任务上出现 20% 的误放弃
    # （§11.9c）。这条把「误放弃」的后果从「任务没了」降级成「打扰人一次」。
    # 注意在 AutoApproveGate 下这条**看不出效果**（网关直接采纳 LLM 裁决），
    # 它改变的是真实介入配置下的行为，因此实测数据无法验证它 —— 依据是结构性的。
    if (
        policy.escalate_on_abandon
        and verdict is not None
        and verdict.action == "ABANDON"
    ):
        return "决策是 ABANDON —— 放弃对该任务不可逆，需人确认"

    # 3. 触及用户原始意图
    if (
        policy.escalate_on_toplevel_modify
        and spec.parent_id is None
        and verdict is not None
        and verdict.action in ("MODIFY_TASK", "ABANDON")
    ):
        return f"要对 parent_id 为空的顶层任务执行 {verdict.action}"

    # 4. 已越界，需人确认边界是否该扩
    # 但验收 command 撞白名单（payload.during == "acceptance"）不是「Subagent 越界
    # 要扩边界」—— 那是架构师自己生成的验收脚本写错了程序（test/sh 这类 shell
    # 命令），该让架构师决策换 command（modified_criteria），而不是升级给人：
    # 人那边没有换 command 的入口，只会反复「改一下任务」而 spec 纹丝不动（真人反馈）。
    if policy.escalate_on_scope_violation and any(
        s.type is SignalType.SCOPE_VIOLATION
        and not (
            isinstance(s.payload, dict) and s.payload.get("during") == "acceptance"
        )
        for s in signals
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
        # modified_criteria 同样能塞进一条命令，不可逆标记要一并查 —— 否则
        # 「替换已有验收 command」就成了一条绕过这条判据的通道。
        for crit in verdict.spec_changes.get("modified_criteria", []):
            haystack.extend(crit.get("command", []) or [])
    for token in haystack:
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in policy.irreversible_markers:
            return base
    return None


def deterministic_plan_escalation(
    policy: Policy,
    *,
    attempt: int,
    fingerprints: list[str],
) -> str | None:
    """拆解层的确定性升级下限（§12 M7 7.4）。

    **和上面那套是同一组判据，只是换了一层**：执行层「同样的信号又原样出现」
    对应拆解层「同样的复核结论又原样出现」，执行层的 `max_rebase` 对应拆解层的
    `max_regenerate`。所以它放在这个模块里，而不是在 architect 里另起一套 ——
    §12 M7 明写「发现自己在写平行逻辑就是方向错了」。

    attempt：已经生成了第几轮（含本轮）。
    fingerprints：每轮复核结论的指纹，按时间顺序。
    """
    # 1. 重生成没有改变现实：复核结论一字不变地又来一遍。
    # 与 §7.2 第 1b 条同源 —— 那条管「改了规格但信号照旧」，这条管
    # 「按复核意见重拆了，复核意见照旧」。都是「上一次决策没有奏效」。
    #
    # **实测它在这一层几乎是死的**（§11.13）：16 次重生成里第二轮的缺口
    # 16/16 都和第一轮不同，这条一次都没触发，兜底的全是下面的次数上限。
    # 原因是结构性的 —— 执行层的指纹看的是「同一个失败信号原样重现」，
    # 而这里复核者每轮看到的是**一份不同的拆解**，措辞必然变。
    # 要让它有判别力得先把 findings 归一化成语义键，那是另一件事；
    # 在那之前别把它当主力（同 §11.6c 对软信号批处理阈值的处置：测不出来就
    # 别编一个数，最后那两个参数直接删了）。
    streak = 1
    for past in reversed(fingerprints[:-1]):
        if past != fingerprints[-1]:
            break
        streak += 1
    if len(fingerprints) >= 2 and streak >= policy.max_identical_interrupts:
        return (
            f"连续 {streak} 轮复核结论完全相同"
            f"（阈值 {policy.max_identical_interrupts}），重生成没有改变现实"
        )

    # 2. 重生成次数用尽。上限本身的取值理由见 policy.max_regenerate。
    if attempt > policy.max_regenerate:
        return (
            f"已重生成 {attempt - 1} 次仍未通过复核"
            f"（阈值 max_regenerate={policy.max_regenerate}）"
        )
    return None


def should_escalate(
    policy: Policy,
    spec: TaskSpec,
    state: TaskState,
    signals: list[Signal],
    verdict: ArchitectVerdict,
    *,
    identical_streak: int = 1,
) -> str | None:
    """确定性下限 OR LLM 自评超阈值。"""
    hard = deterministic_escalation(
        policy, spec, state, signals, verdict, identical_streak=identical_streak
    )
    if hard:
        return hard
    if verdict.complexity_score >= policy.complexity_threshold:
        return (
            f"LLM 自评 complexity_score={verdict.complexity_score:.2f}"
            f" >= 阈值 {policy.complexity_threshold}"
        )
    return None
