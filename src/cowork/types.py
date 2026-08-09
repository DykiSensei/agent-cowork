"""核心数据结构（文档 §4）。

序列化约定：所有 to_dict 返回纯 JSON 可编码的 dict。
AgentContext 的 produced / reasoning_trace **必须**是两个顶层键——
这是 REBASE 能低成本工作的全部前提（§10.5）。
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any

from . import ids
from .signals import SignalLevel, SignalSource, SignalType, default_hard_signals


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class TaskClass(str, enum.Enum):
    CODE = "CODE"
    TOOL_CALL = "TOOL_CALL"
    GENERATIVE = "GENERATIVE"


class SilencePolicy(str, enum.Enum):
    TRUST = "TRUST"
    PROBE = "PROBE"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class Disposition(str, enum.Enum):
    PREEMPTED = "PREEMPTED"
    ESCALATED = "ESCALATED"
    IGNORED = "IGNORED"
    QUEUED = "QUEUED"


class Action(str, enum.Enum):
    CONTINUE = "CONTINUE"
    MODIFY_TASK = "MODIFY_TASK"
    ABANDON = "ABANDON"
    REASSIGN = "REASSIGN"


class ResumeMode(str, enum.Enum):
    RESUME = "RESUME"
    REBASE = "REBASE"
    RESTART = "RESTART"


class Decider(str, enum.Enum):
    LLM = "LLM"
    HUMAN = "HUMAN"


# --------------------------------------------------------------------------- #
# 验收标准
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Criterion:
    """验收标准。

    command 非空 -> Runtime 可确定性检查，失败产生 TEST_FAILED（L0）。
    command 为空 -> 只能由架构师（LLM/人）判定，不产生硬信号。
    """

    id: str
    description: str
    command: list[str] | None = None

    @property
    def machine_checkable(self) -> bool:
        return bool(self.command)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "command": self.command}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Criterion:
        return cls(id=d["id"], description=d["description"], command=d.get("command"))


@dataclass(frozen=True)
class SandboxProfile:
    """v0.1：本地 Docker 或本地子进程，只为验证 SCOPE_VIOLATION 语义（§10.4）。"""

    workspace: str
    allowed_binaries: tuple[str, ...] = ("python", "python3")
    use_docker: bool = False
    image: str = "python:3.12-slim"
    network: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "allowed_binaries": list(self.allowed_binaries),
            "use_docker": self.use_docker,
            "image": self.image,
            "network": self.network,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SandboxProfile:
        return cls(
            workspace=d["workspace"],
            allowed_binaries=tuple(d.get("allowed_binaries", ("python", "python3"))),
            use_docker=d.get("use_docker", False),
            image=d.get("image", "python:3.12-slim"),
            network=d.get("network", "none"),
        )


# --------------------------------------------------------------------------- #
# TaskSpec（§4.1）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskSpec:
    goal: str
    acceptance: list[Criterion]
    task_class: TaskClass

    id: str = field(default_factory=ids.task_id)
    parent_id: str | None = None
    revision: int = 1

    output_schema: dict[str, Any] = field(default_factory=dict)

    hard_signals: frozenset[SignalType] = field(default=frozenset())
    silence_policy: SilencePolicy = SilencePolicy.TRUST
    probe_interval_s: float | None = None

    model: str = "claude-opus-5"
    sandbox: SandboxProfile | None = None
    scope: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    deadline_s: float = 120.0
    max_steps: int = 12
    token_budget: int = 200_000

    context_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # acceptance 必填是刻意的硬约束（§4.1）：写不出验收标准的任务，
        # 说明拆解本身没想清楚，不应该派发。
        if not self.acceptance:
            raise ValueError("TaskSpec.acceptance 至少要有一条")

        # task_class = GENERATIVE 时 silence_policy 强制为 PROBE，
        # 由 Runtime 在构造时校验，架构师无权设为 TRUST（§4.1）。
        if self.task_class is TaskClass.GENERATIVE:
            object.__setattr__(self, "silence_policy", SilencePolicy.PROBE)
        if self.silence_policy is SilencePolicy.PROBE and not self.probe_interval_s:
            raise ValueError("silence_policy=PROBE 时 probe_interval_s 必填")

        if self.task_class is TaskClass.CODE and self.sandbox is None:
            raise ValueError("task_class=CODE 时 sandbox 必填")

        if not self.hard_signals:
            object.__setattr__(
                self, "hard_signals", default_hard_signals(self.task_class.value)
            )

    def bump(self, **changes: Any) -> TaskSpec:
        """生成 revision+1 的新 spec。改任务的唯一入口。"""
        from dataclasses import replace

        changes.setdefault("revision", self.revision + 1)
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "revision": self.revision,
            "goal": self.goal,
            "acceptance": [c.to_dict() for c in self.acceptance],
            "output_schema": self.output_schema,
            "task_class": self.task_class.value,
            "hard_signals": sorted(s.value for s in self.hard_signals),
            "silence_policy": self.silence_policy.value,
            "probe_interval_s": self.probe_interval_s,
            "model": self.model,
            "sandbox": self.sandbox.to_dict() if self.sandbox else None,
            "scope": list(self.scope),
            "tools": list(self.tools),
            "deadline_s": self.deadline_s,
            "max_steps": self.max_steps,
            "token_budget": self.token_budget,
            "context_refs": list(self.context_refs),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskSpec:
        return cls(
            id=d["id"],
            parent_id=d.get("parent_id"),
            revision=d.get("revision", 1),
            goal=d["goal"],
            acceptance=[Criterion.from_dict(c) for c in d["acceptance"]],
            output_schema=d.get("output_schema", {}),
            task_class=TaskClass(d["task_class"]),
            hard_signals=frozenset(SignalType(s) for s in d.get("hard_signals", [])),
            silence_policy=SilencePolicy(d.get("silence_policy", "TRUST")),
            probe_interval_s=d.get("probe_interval_s"),
            model=d.get("model", "claude-opus-5"),
            sandbox=SandboxProfile.from_dict(d["sandbox"]) if d.get("sandbox") else None,
            scope=list(d.get("scope", [])),
            tools=list(d.get("tools", [])),
            deadline_s=d.get("deadline_s", 120.0),
            max_steps=d.get("max_steps", 12),
            token_budget=d.get("token_budget", 200_000),
            context_refs=list(d.get("context_refs", [])),
            depends_on=list(d.get("depends_on", [])),
        )


# --------------------------------------------------------------------------- #
# Artifact / AgentContext（§4.5）
# --------------------------------------------------------------------------- #


@dataclass
class Artifact:
    task_id: str
    kind: str
    content_ref: str
    summary: str = ""
    id: str = field(default_factory=ids.artifact_id)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "content_ref": self.content_ref,
            "summary": self.summary,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Artifact:
        return cls(
            id=d["id"],
            task_id=d["task_id"],
            kind=d["kind"],
            content_ref=d["content_ref"],
            summary=d.get("summary", ""),
            created_at=d.get("created_at", time.time()),
        )


@dataclass
class AgentContext:
    """显式区分「该保留的成果」和「该丢弃的过程」。

    produced 与 reasoning_trace 在序列化后是两个顶层键，REBASE 时直接丢弃后者、
    不做任何解析。存成一坨扁平消息列表的话，§6 整节都无法实现。
    """

    task_spec: TaskSpec
    injected: list[Artifact] = field(default_factory=list)
    produced: list[Artifact] = field(default_factory=list)
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_spec": self.task_spec.to_dict(),
            "injected": [a.to_dict() for a in self.injected],
            "produced": [a.to_dict() for a in self.produced],
            "reasoning_trace": list(self.reasoning_trace),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentContext:
        return cls(
            task_spec=TaskSpec.from_dict(d["task_spec"]),
            injected=[Artifact.from_dict(a) for a in d.get("injected", [])],
            produced=[Artifact.from_dict(a) for a in d.get("produced", [])],
            reasoning_trace=list(d.get("reasoning_trace", [])),
            summary=d.get("summary"),
        )


# --------------------------------------------------------------------------- #
# Signal / Checkpoint / TaskState / DecisionRecord
# --------------------------------------------------------------------------- #


@dataclass
class Signal:
    type: SignalType
    task_id: str
    source: SignalSource
    level: SignalLevel = SignalLevel.L0
    payload: dict[str, Any] = field(default_factory=dict)
    raw_evidence: str | None = None
    id: str = field(default_factory=ids.signal_id)
    created_at: float = field(default_factory=time.time)
    consumed_at: float | None = None
    disposition: Disposition = Disposition.QUEUED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level.value,
            "type": self.type.value,
            "task_id": self.task_id,
            "source": self.source.value,
            "payload": self.payload,
            "raw_evidence": self.raw_evidence,
            "created_at": self.created_at,
            "consumed_at": self.consumed_at,
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Signal:
        return cls(
            id=d["id"],
            level=SignalLevel(d["level"]),
            type=SignalType(d["type"]),
            task_id=d["task_id"],
            source=SignalSource(d["source"]),
            payload=d.get("payload", {}),
            raw_evidence=d.get("raw_evidence"),
            created_at=d.get("created_at", time.time()),
            consumed_at=d.get("consumed_at"),
            disposition=Disposition(d.get("disposition", "QUEUED")),
        )


@dataclass
class Checkpoint:
    task_id: str
    step: int
    agent_context: AgentContext
    artifacts: list[str] = field(default_factory=list)
    id: str = field(default_factory=ids.checkpoint_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class TaskState:
    spec: TaskSpec
    status: TaskStatus = TaskStatus.PENDING
    agent_id: str | None = None
    current_step: int = 0
    checkpoint_id: str | None = None
    interrupt_count: int = 0
    artifacts: list[str] = field(default_factory=list)
    signal_log: list[str] = field(default_factory=list)
    tokens_used: int = 0
    started_at: float | None = None


@dataclass
class DecisionRecord:
    trigger: list[str]
    decider: Decider
    action: Action
    rationale: str
    task_id: str
    complexity_score: float | None = None
    escalation_reason: str | None = None
    new_spec: TaskSpec | None = None
    resume_mode: ResumeMode | None = None
    id: str = field(default_factory=ids.decision_id)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "trigger": list(self.trigger),
            "decider": self.decider.value,
            "complexity_score": self.complexity_score,
            "escalation_reason": self.escalation_reason,
            "action": self.action.value,
            "new_spec": self.new_spec.to_dict() if self.new_spec else None,
            "resume_mode": self.resume_mode.value if self.resume_mode else None,
            "rationale": self.rationale,
            "created_at": self.created_at,
        }
