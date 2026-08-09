"""改任务与恢复（§6）。

改了 TaskSpec 之后直接续跑是错误的：reasoning_trace 里还留着旧任务的推理痕迹，
模型会被旧目标带偏。REBASE 存在的全部理由就是避免这种污染。
"""

from __future__ import annotations

from .types import AgentContext, Artifact, ResumeMode, TaskSpec


def choose_resume_mode(old: TaskSpec, new: TaskSpec) -> ResumeMode:
    """§6.2 的选择规则，逐条落地。"""
    if new.revision == old.revision:
        return ResumeMode.RESUME

    goal_changed = new.goal.strip() != old.goal.strip()
    if not goal_changed:
        # 仅 acceptance / scope / budget 变化 -> REBASE（默认）
        return ResumeMode.REBASE

    # goal 实质变化：produced 还有价值就 REBASE（转为只读上下文），否则 RESTART
    return ResumeMode.REBASE if _produced_still_useful(old, new) else ResumeMode.RESTART


def _produced_still_useful(old: TaskSpec, new: TaskSpec) -> bool:
    """启发式：scope 有交集就认为旧产出仍可作参考。

    v0.1 用规则；真跑起来之后这里大概率要换成架构师判断。
    """
    return bool(set(old.scope) & set(new.scope))


def rebase(
    ctx: AgentContext,
    new_spec: TaskSpec,
    backend,
) -> tuple[AgentContext, int]:
    """§6.3 REBASE 的执行。

    1. 从 checkpoint 取出 AgentContext（调用方已做）
    2. 对 produced 生成压缩摘要 —— 这一步本身消耗 token，需计入预算
    3. 构造新的 AgentContext
    4. 由调用方起新的 Subagent 实例执行
    """
    summary, tokens = backend.summarize(ctx)

    digest = Artifact(
        task_id=new_spec.id,
        kind="summary",
        content_ref=f"rebase-summary@rev{new_spec.revision}",
        summary=summary,
    )

    new_ctx = AgentContext(
        task_spec=new_spec,
        injected=[*ctx.injected, digest],
        produced=list(ctx.produced),   # 保留原产出的引用
        reasoning_trace=[],            # 清空 —— 这是 REBASE 的全部意义
        summary=summary,
    )
    return new_ctx, tokens


def restart(ctx: AgentContext, new_spec: TaskSpec, *, keep_produced_as_reference: bool = True):
    """方向完全错误，成果不可用。可选保留 produced 作只读参考。"""
    injected = list(ctx.produced) if keep_produced_as_reference else []
    return AgentContext(
        task_spec=new_spec,
        injected=injected,
        produced=[],
        reasoning_trace=[],
        summary=None,
    )


def resume(ctx: AgentContext, new_spec: TaskSpec) -> AgentContext:
    """任务未变，只是补充信息或重试瞬时故障。全部上下文保留。"""
    ctx.task_spec = new_spec
    return ctx


def apply_resume(
    mode: ResumeMode,
    ctx: AgentContext,
    new_spec: TaskSpec,
    backend,
) -> tuple[AgentContext, int]:
    if mode is ResumeMode.RESUME:
        return resume(ctx, new_spec), 0
    if mode is ResumeMode.REBASE:
        return rebase(ctx, new_spec, backend)
    if mode is ResumeMode.RESTART:
        return restart(ctx, new_spec), 0
    raise ValueError(mode)
