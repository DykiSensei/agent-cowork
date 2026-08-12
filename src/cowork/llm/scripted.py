"""脚本化后端 —— 确定性，无网络，无 API key。

存在的理由：v0.1 要验证的是**控制流**（L0 信号 -> 中断 -> REBASE -> 恢复），
不是模型能力。把模型换成脚本，链路就变成可断言的，测试才有意义。
真实模型跑同一条链路用 AnthropicBackend，接口完全一致。
"""

from __future__ import annotations

from typing import Any, Callable

from ..actions import AgentAction, Finish
from ..llm import ArchitectVerdict, Triage
from ..types import AgentContext, Signal, TaskSpec

# (revision, step_index_within_revision) -> action
StepScript = dict[tuple[int, int], AgentAction]


class ScriptedBackend:
    name = "scripted"

    def __init__(
        self,
        steps: StepScript,
        *,
        verdict_for: Callable[[TaskSpec, list[Signal]], ArchitectVerdict] | None = None,
        triage_for: Callable[[Signal], str] | None = None,
        probe_for: Callable[[TaskSpec, AgentContext], tuple[bool, str]] | None = None,
        review_for: Callable[[str, list], tuple[bool, list[str]]] | None = None,
        spec_review_for: Callable[[TaskSpec, list[Signal], Any], tuple[bool, list[str]]]
        | None = None,
        decompose_for: Callable[[str, list[str] | None], list] | None = None,
        token_cost: int = 500,
    ) -> None:
        self.steps = steps
        self.verdict_for = verdict_for
        self.triage_for = triage_for or (lambda s: "escalate")
        self.probe_for = probe_for
        self.review_for = review_for
        self.spec_review_for = spec_review_for
        self.decompose_for = decompose_for
        self.token_cost = token_cost
        self._cursor: dict[int, int] = {}
        self.probe_calls = 0
        self.profile_calls = 0
        self.review_calls = 0
        self.spec_review_calls = 0
        self.decompose_calls = 0
        self.decompose_feedback: list[list[str] | None] = []
        self.decompose_existing: list[str | None] = []
        self.decompose_environment: list[str | None] = []
        self.decide_feedback: list[list[str] | None] = []
        self.decide_history: list[dict] = []

    # -- Subagent ---------------------------------------------------------- #

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        rev = ctx.task_spec.revision
        i = self._cursor.get(rev, 0)
        self._cursor[rev] = i + 1
        action = self.steps.get((rev, i))
        if action is None:
            action = Finish(
                output={}, summary="脚本已耗尽", thought="ScriptedBackend: 无更多动作"
            )
        return action, self.token_cost

    # -- 架构师 ------------------------------------------------------------ #

    def triage(self, signals: list[Signal]) -> tuple[list[Triage], int]:
        out = [Triage(s.id, self.triage_for(s), "scripted") for s in signals]
        return out, self.token_cost // 10 * max(1, len(signals))

    def decide_interrupt(
        self,
        spec: TaskSpec,
        signals: list[Signal],
        ctx: AgentContext,
        *,
        history: list[dict] | None = None,
        review_feedback: list[str] | None = None,
    ) -> tuple[ArchitectVerdict, int]:
        self.decide_history = list(history or [])  # 测试用：确认历史真的传进来了
        # 每一轮拿到的复核意见：写入侧的生成-复核循环要断言「意见真的喂回去了」，
        # 那是它和「每轮从零重做」的唯一区别（同 decompose_feedback）
        self.decide_feedback.append(list(review_feedback) if review_feedback else None)
        if self.verdict_for is None:
            return (
                ArchitectVerdict(
                    action="ABANDON",
                    rationale="ScriptedBackend 未提供 verdict_for",
                    complexity_score=1.0,
                ),
                self.token_cost,
            )
        return self.verdict_for(spec, signals), self.token_cost

    def summarize(self, ctx: AgentContext) -> tuple[str, int]:
        files = ", ".join(a.content_ref for a in ctx.produced) or "无"
        return f"已产出：{files}", self.token_cost // 5

    def verify(self, spec: TaskSpec, ctx: AgentContext) -> tuple[bool, str, int]:
        return True, "脚本后端：非机器可检项默认通过", self.token_cost // 10

    def review_decomposition(
        self, root_goal: str, specs: list[TaskSpec]
    ) -> tuple[bool, list[str], int]:
        self.review_calls += 1
        if self.review_for is None:
            return True, [], self.token_cost // 5
        sufficient, missing = self.review_for(root_goal, specs)
        return sufficient, list(missing), self.token_cost // 5

    def review_spec_change(
        self, spec: TaskSpec, signals: list[Signal], verdict
    ) -> tuple[bool, list[str], int]:
        self.spec_review_calls += 1
        if self.spec_review_for is None:
            return True, [], self.token_cost // 5
        ok, findings = self.spec_review_for(spec, signals, verdict)
        return ok, list(findings), self.token_cost // 5

    def decompose(
        self, root_goal: str, *, feedback: list[str] | None = None,
        existing: str | None = None, environment: str | None = None,
    ) -> tuple[list, int]:
        # 记下每一轮拿到的 feedback：生成-复核循环要断言「复核意见真的喂回去了」，
        # 那是 7.4 与「每轮都从零开始重拆」的唯一区别。
        # existing 同理：接手已有项目时要能断言「现状真的送到生成者手上了」。
        self.decompose_calls += 1
        self.decompose_feedback.append(list(feedback) if feedback else None)
        self.decompose_existing.append(existing)
        self.decompose_environment.append(environment)
        if self.decompose_for is None:
            raise NotImplementedError("ScriptedBackend 未提供 decompose_for")
        return self.decompose_for(root_goal, feedback), self.token_cost

    def profile_tasks(self, specs: list[TaskSpec]) -> tuple[list, int]:
        from ..llm import TaskProfile

        self.profile_calls += 1
        return [TaskProfile(s.id, "other", s.goal[:40], []) for s in specs], self.token_cost // 5

    def probe(
        self, spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]
    ) -> tuple[bool, str, int]:
        self.probe_calls += 1
        if self.probe_for is None:
            return True, "脚本后端：中间产出默认在轨", self.token_cost // 5
        on_track, reason = self.probe_for(spec, ctx, excerpts)
        return on_track, reason, self.token_cost // 5
