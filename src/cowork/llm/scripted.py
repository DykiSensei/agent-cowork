"""脚本化后端 —— 确定性，无网络，无 API key。

存在的理由：v0.1 要验证的是**控制流**（L0 信号 -> 中断 -> REBASE -> 恢复），
不是模型能力。把模型换成脚本，链路就变成可断言的，测试才有意义。
真实模型跑同一条链路用 AnthropicBackend，接口完全一致。
"""

from __future__ import annotations

from typing import Callable

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
        token_cost: int = 500,
    ) -> None:
        self.steps = steps
        self.verdict_for = verdict_for
        self.triage_for = triage_for or (lambda s: "escalate")
        self.probe_for = probe_for
        self.token_cost = token_cost
        self._cursor: dict[int, int] = {}
        self.probe_calls = 0

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
        self, spec: TaskSpec, signals: list[Signal], ctx: AgentContext
    ) -> tuple[ArchitectVerdict, int]:
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

    def probe(
        self, spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]
    ) -> tuple[bool, str, int]:
        self.probe_calls += 1
        if self.probe_for is None:
            return True, "脚本后端：中间产出默认在轨", self.token_cost // 5
        on_track, reason = self.probe_for(spec, ctx, excerpts)
        return on_track, reason, self.token_cost // 5
