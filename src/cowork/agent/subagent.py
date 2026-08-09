"""Subagent（§2.2）。

临时、任务隔离、可并行。不持有跨任务的记忆——每次派发时由架构师注入完整上下文。
只能与架构师通信；产生的信号一律经架构师中转。可主动发软信号，
但无权要求立即中断。

这个类薄得几乎没有逻辑，是刻意的：Subagent 的「智能」全在 backend 里，
它的「权限」全在 Runtime 里。中间这层只做绑定。
"""

from __future__ import annotations

from .. import ids
from ..actions import AgentAction
from ..llm import Backend
from ..types import AgentContext


class Subagent:
    def __init__(self, backend: Backend, agent_id: str | None = None) -> None:
        self.backend = backend
        self.id = agent_id or ids.agent_id()

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        return self.backend.next_step(ctx)
