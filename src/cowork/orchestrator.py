"""编排：把 §5 的状态机跑起来。

    RUNNING -> (L0 抢占 | 验收不通过) -> INTERRUPTED -> 架构师决策
            -> CONTINUE / MODIFY_TASK / REASSIGN -> 选恢复模式 -> 从 checkpoint 恢复
            -> RUNNING ...

v0.1 只覆盖单个任务的这一条链路。并行、冲突检测、界面层状态同步不在范围内。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent.architect import Architect, HumanGate
from .agent.subagent import Subagent
from .llm import Backend
from .llm.errors import ModelError
from .policy import DEFAULT_POLICY, Policy
from .resume import apply_resume
from .runtime.bus import SignalBus
from .runtime.loop import StepLoop
from .runtime.sandbox import Sandbox
from .signals import SignalSource, SignalType
from .types import (
    Action,
    AgentContext,
    Artifact,
    DecisionRecord,
    ResumeMode,
    SilencePolicy,
    Signal,
    TaskSpec,
    TaskState,
    TaskStatus,
)


@dataclass
class RunResult:
    state: TaskState
    context: AgentContext
    decisions: list[DecisionRecord] = field(default_factory=list)
    output: dict | None = None


class Orchestrator:
    def __init__(
        self,
        spec: TaskSpec,
        *,
        backend: Backend,
        store,
        policy: Policy = DEFAULT_POLICY,
        human_gate: HumanGate | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        if spec.silence_policy is SilencePolicy.PROBE:
            raise NotImplementedError(
                "silence_policy=PROBE（GENERATIVE 类任务）不在 v0.1 链路内。"
                "见开发文档 §11 第 3 条：PROBE 的 token 成本需先实测。"
            )
        if spec.sandbox is None:
            raise ValueError("v0.1 需要 sandbox（CODE 类任务）")

        self.spec = spec
        self.backend = backend
        self.store = store
        self.policy = policy
        self.log = log

        self.bus = SignalBus()
        self.sandbox = Sandbox(spec.sandbox, spec.scope)
        self.architect = Architect(backend, store, policy=policy, human_gate=human_gate)
        self.loop = StepLoop(bus=self.bus, sandbox=self.sandbox, store=store)

        self.state = TaskState(spec=spec, status=TaskStatus.PENDING)
        self.ctx = AgentContext(task_spec=spec)
        self.decisions: list[DecisionRecord] = []
        self._rebase_count = 0

    # ------------------------------------------------------------------ #

    def inject(self, artifacts: list[Artifact]) -> None:
        """架构师注入只读上下文（§8：传引用不传全文）。"""
        for a in artifacts:
            self.store.save_artifact(a)
        self.ctx.injected.extend(artifacts)

    def intervene(self, instruction: str) -> Signal:
        """人在群聊中介入 -> HUMAN_INTERVENTION 硬信号 -> 下个 step 边界立即抢占。"""
        sig = self.bus.human_intervention(self.spec.id, instruction)
        self.store.save_signal(sig)
        self.log(f"[HUMAN] 介入: {instruction}")
        return sig

    # ------------------------------------------------------------------ #

    def run(self, max_cycles: int = 8) -> RunResult:
        self.state.started_at = time.time()
        self.store.save_task(self.state)

        for cycle in range(1, max_cycles + 1):
            self.state.status = TaskStatus.RUNNING
            subagent = Subagent(self.backend)
            self.state.agent_id = subagent.id
            self.store.save_task(self.state)
            self.log(
                f"[RUN ] cycle={cycle} rev={self.ctx.task_spec.revision} "
                f"agent={subagent.id} step={self.state.current_step}"
            )

            outcome = self.loop.run(
                self.ctx,
                subagent,
                start_step=self.state.current_step,
                tokens_used=self.state.tokens_used,
            )
            self.ctx = outcome.context
            self.state.current_step += outcome.steps_run
            self.state.tokens_used = outcome.tokens_used
            self.state.checkpoint_id = outcome.checkpoint_id
            self.state.artifacts = [a.id for a in self.ctx.produced]

            # 软信号在检查点批量消费（§3.4）
            if outcome.soft_signals:
                for s in outcome.soft_signals:
                    self.store.save_signal(s)
                escalated = self.architect.consume_soft(outcome.soft_signals)
                self.log(
                    f"[SOFT] 消费 {len(outcome.soft_signals)} 条，"
                    f"升级 {len(escalated)} 条"
                )

            if outcome.status is TaskStatus.COMPLETED:
                passed, reason = self.architect.verify(self.ctx.task_spec, self.ctx)
                if passed:
                    self.state.status = TaskStatus.COMPLETED
                    self.store.save_task(self.state)
                    self.log(f"[DONE] {reason}")
                    return RunResult(self.state, self.ctx, self.decisions, outcome.output)

                # 验收不通过 -> 当作 L0 信号处理（§5 流程图右下角）
                self.log(f"[FAIL] 架构师验收不通过: {reason}")
                sig = self.bus.emit_hard(
                    SignalType.VALIDATION_FAILED,
                    self.spec.id,
                    payload={"origin": "architect_verify"},
                    evidence=reason,
                )
                self.store.save_signal(sig)
                triggers = [sig]
            else:
                triggers = list(outcome.preempting_signals)

            # ---- INTERRUPTED ----
            self.state.status = TaskStatus.INTERRUPTED
            self.state.interrupt_count += 1
            self.state.signal_log.extend(s.id for s in triggers)
            self.store.save_task(self.state)
            self.log(
                f"[STOP] {triggers[0].type.value if triggers else '未知'} "
                f"@step={self.state.current_step} interrupt_count={self.state.interrupt_count}"
            )

            try:
                decision = self.architect.decide(self.state, triggers, self.ctx)
            except ModelError as exc:
                # 架构师自己也调不动模型了（典型场景：virtual key 预算耗尽，
                # Subagent 和架构师用同一把 key）。没有决策者，只能挂起等人。
                self.state.status = TaskStatus.AWAITING_HUMAN
                self.store.save_task(self.state)
                self.log(f"[STOP] 架构师无法决策（{exc.signal_type.value}）: {exc.message[:200]}")
                return RunResult(self.state, self.ctx, self.decisions)

            self.store.save_decision(decision)
            self.decisions.append(decision)
            self._render(decision)

            if decision.action is Action.ABANDON:
                self.state.status = TaskStatus.ABANDONED
                self.store.save_task(self.state)
                return RunResult(self.state, self.ctx, self.decisions)

            if decision.resume_mode is None or decision.new_spec is None:
                self.state.status = TaskStatus.AWAITING_HUMAN
                self.store.save_task(self.state)
                return RunResult(self.state, self.ctx, self.decisions)

            if decision.resume_mode is ResumeMode.REBASE:
                self._rebase_count += 1
                if self._rebase_count > self.policy.max_rebase:
                    # 风险 #5：多次 REBASE 后摘要压缩会累积失真
                    self.state.status = TaskStatus.AWAITING_HUMAN
                    self.store.save_task(self.state)
                    self.log(
                        f"[STOP] REBASE 次数超过上限 {self.policy.max_rebase}，挂起等人"
                    )
                    return RunResult(self.state, self.ctx, self.decisions)

            self.ctx, tokens = apply_resume(
                decision.resume_mode, self.ctx, decision.new_spec, self.backend
            )
            self.state.tokens_used += tokens
            self.state.spec = decision.new_spec
            self.spec = decision.new_spec
            self.sandbox = Sandbox(decision.new_spec.sandbox, decision.new_spec.scope)
            self.loop = StepLoop(bus=self.bus, sandbox=self.sandbox, store=self.store)
            self.store.save_task(self.state)

        self.state.status = TaskStatus.FAILED
        self.store.save_task(self.state)
        self.log(f"[STOP] 超过 max_cycles={max_cycles}")
        return RunResult(self.state, self.ctx, self.decisions)

    # ------------------------------------------------------------------ #

    def _render(self, d: DecisionRecord) -> None:
        """§7.3：每条 DecisionRecord 都渲染出来，LLM 决策与人的决策同等展示。"""
        self.log(
            f"[DEC ] decider={d.decider.value} action={d.action.value} "
            f"resume={d.resume_mode.value if d.resume_mode else '-'} "
            f"complexity={d.complexity_score if d.complexity_score is not None else '-'}"
        )
        if d.escalation_reason:
            self.log(f"       升级原因: {d.escalation_reason}")
        self.log(f"       理由: {d.rationale}")
        if d.new_spec:
            self.log(f"       新 spec: rev={d.new_spec.revision} goal={d.new_spec.goal[:60]}")
