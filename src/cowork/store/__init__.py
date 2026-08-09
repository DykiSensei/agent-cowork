"""持久化层。

五张表（§10.5）：tasks / checkpoints / signals / decisions / artifacts。
Store 是协议，有两个实现：
  - SqliteStore   零依赖，跑通链路与测试用
  - PostgresStore §10.2 的正式选型
两者写的 checkpoints.context_json 结构完全一致：produced 与 reasoning_trace
是两个顶层键。这是唯一不能将就的地方。
"""

from __future__ import annotations

from typing import Protocol

from ..types import Artifact, Checkpoint, DecisionRecord, Signal, TaskState


class Store(Protocol):
    def save_task(self, state: TaskState) -> None: ...
    def load_task(self, task_id: str) -> TaskState | None: ...
    def list_tasks(self) -> list[TaskState]: ...

    def save_checkpoint(self, cp: Checkpoint) -> str: ...
    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint | None: ...

    def save_signal(self, sig: Signal) -> None: ...
    def signals_for(self, task_id: str) -> list[Signal]: ...

    def save_decision(self, dec: DecisionRecord) -> None: ...
    def decisions_for(self, task_id: str) -> list[DecisionRecord]: ...

    def save_artifact(self, art: Artifact) -> None: ...
    def load_artifact(self, artifact_id: str) -> Artifact | None: ...

    def close(self) -> None: ...


from .sqlite import SqliteStore  # noqa: E402

__all__ = ["Store", "SqliteStore"]
