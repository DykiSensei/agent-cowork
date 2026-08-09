"""模型后端协议。

三个角色各自需要模型，但需求不同：
  - Subagent  决定下一个 step
  - 架构师主模型  中断决策 / 验收
  - 廉价评估模型  软信号批量分诊（§3.4）
后端把三者收在一个协议里，实现方决定用哪个模型跑哪个角色
（TaskSpec.model 承载「不同模型干擅长的事」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..actions import AgentAction
from ..types import AgentContext, Signal, TaskSpec


@dataclass
class Triage:
    signal_id: str
    verdict: Literal["ignore", "escalate"]
    reason: str = ""


@dataclass
class ArchitectVerdict:
    """架构师对一次中断的裁决。resume_mode 留空则由 §6.2 规则推导。"""

    action: str  # CONTINUE | MODIFY_TASK | ABANDON | REASSIGN
    rationale: str
    complexity_score: float = 0.0
    spec_changes: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        """Subagent 视角：给定上下文，决定下一步。返回 (动作, token 消耗)。"""
        ...

    def triage(self, signals: list[Signal]) -> tuple[list[Triage], int]:
        """廉价批量评估（§3.4）：只输出 ignore | escalate。"""
        ...

    def decide_interrupt(
        self, spec: TaskSpec, signals: list[Signal], ctx: AgentContext
    ) -> tuple[ArchitectVerdict, int]:
        """架构师主模型：完整中断决策。"""
        ...

    def summarize(self, ctx: AgentContext) -> tuple[str, int]:
        """REBASE 第 2 步：对 produced 生成压缩摘要（本身消耗 token，计入预算）。"""
        ...

    def verify(self, spec: TaskSpec, ctx: AgentContext) -> tuple[bool, str, int]:
        """架构师验收非机器可检的验收标准。"""
        ...


from .scripted import ScriptedBackend  # noqa: E402

__all__ = ["Backend", "Triage", "ArchitectVerdict", "ScriptedBackend"]
