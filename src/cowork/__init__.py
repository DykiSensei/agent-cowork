"""多 Agent 协作系统 —— v0.1 原型。

验证的唯一链路：L0 信号 -> 中断 -> REBASE -> 恢复（开发文档 §11 第 1 条）。

架构不变量：
  1. 执行层中心化 —— Subagent 只与架构师通信，彼此不通信
  2. Runtime 不含 LLM —— 所有硬信号由确定性检测产生
  3. step 循环自己持有 —— 外部抢占 = step 边界不派发下一个 step
  4. produced 与 reasoning_trace 在 checkpoint 里是两个顶层键
"""

from .orchestrator import Orchestrator, RunResult
from .policy import DEFAULT_POLICY, Policy
from .store import SqliteStore
from .types import (
    AgentContext,
    Artifact,
    Criterion,
    SandboxProfile,
    SilencePolicy,
    TaskClass,
    TaskSpec,
    TaskState,
    TaskStatus,
)

__all__ = [
    "Orchestrator",
    "RunResult",
    "Policy",
    "DEFAULT_POLICY",
    "SqliteStore",
    "TaskSpec",
    "TaskState",
    "TaskStatus",
    "TaskClass",
    "SilencePolicy",
    "Criterion",
    "SandboxProfile",
    "AgentContext",
    "Artifact",
]
