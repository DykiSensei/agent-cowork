"""带前缀的 ID 生成。前缀让日志里一眼看出对象类型。"""

from __future__ import annotations

import uuid


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def task_id() -> str:
    return _new("task")


def signal_id() -> str:
    return _new("sig")


def checkpoint_id() -> str:
    return _new("ckpt")


def decision_id() -> str:
    return _new("dec")


def artifact_id() -> str:
    return _new("art")


def agent_id() -> str:
    return _new("agent")


def event_id() -> str:
    return _new("ev")
