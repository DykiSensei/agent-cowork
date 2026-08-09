"""Subagent 每个 step 产出的动作。

一次工具调用 = 一个 step（§5.1 的起步建议）。Finish 是终止动作，
之后 Runtime 才跑验收检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""


@dataclass(frozen=True)
class Finish:
    output: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    thought: str = ""


@dataclass(frozen=True)
class SoftSignalAction:
    """Subagent 主动上报软信号。无权要求立即中断（§2.2）。"""

    signal_type: str
    detail: str


AgentAction = Union[ToolCall, Finish, SoftSignalAction]
