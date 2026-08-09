"""架构师（§2.3）。

单一实例，持有连续上下文，是唯一的写入决策点。
所有跨任务信息只经它流转；Subagent 之间不通信。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from ..escalation import should_escalate
from ..llm import ArchitectVerdict, Backend
from ..plan import deterministic_review
from ..policy import Policy
from ..resume import choose_resume_mode
from ..signals import SignalType, fingerprint
from ..types import (
    Action,
    AgentContext,
    Criterion,
    Decider,
    DecisionRecord,
    Disposition,
    ResumeMode,
    Signal,
    TaskSpec,
    TaskState,
)


@dataclass
class HumanRuling:
    action: Action
    rationale: str
    spec_changes: dict | None = None


@dataclass
class DecompositionReview:
    """拆解复核的结果（§12 M5b）。

    两半分开放是刻意的：`structural` 免费且不会漏判自己，`missing` 来自模型、
    可能有假阳性也可能有假阴性。**混成一个布尔值会把两种可信度不同的证据抹平。**
    """

    structural: list
    sufficient: bool
    missing: list[str]
    tokens: int = 0

    @property
    def clean(self) -> bool:
        return not self.structural and self.sufficient

    def to_dict(self) -> dict:
        return {
            "structural": [
                {"kind": i.kind, "detail": i.detail, "tasks": list(i.tasks)}
                for i in self.structural
            ],
            "sufficient": self.sufficient,
            "missing": list(self.missing),
            "tokens": self.tokens,
        }


class HumanGate(Protocol):
    """人的介入入口。返回 None 表示人还没答复 -> 任务停在 AWAITING_HUMAN。"""

    def review(
        self,
        spec: TaskSpec,
        signals: list[Signal],
        verdict: ArchitectVerdict,
        reason: str,
    ) -> HumanRuling | None: ...


class AutoApproveGate:
    """非交互场景用：直接采纳 LLM 的裁决。

    命名是刻意直白的——它并没有引入人的判断，只是把「有人配置了自动放行」
    这件事显式化。生产环境不要用。
    """

    def review(self, spec, signals, verdict, reason) -> HumanRuling:
        return HumanRuling(
            action=Action(verdict.action),
            rationale=f"[AutoApproveGate] 升级原因：{reason}；采纳 LLM 裁决：{verdict.rationale}",
            spec_changes=verdict.spec_changes,
        )


class CliGate:
    """CLI 介入（v0.1 的界面层，§10.2「暂不做，CLI + 结构化日志」）。"""

    def review(self, spec, signals, verdict, reason) -> HumanRuling | None:
        print("\n" + "=" * 68)
        print(f"需要人决策  task={spec.id} rev={spec.revision}")
        print(f"升级原因    {reason}")
        print(f"触发信号    {', '.join(s.type.value for s in signals)}")
        for s in signals:
            if s.raw_evidence:
                print(f"--- 证据 ({s.type.value}) ---\n{s.raw_evidence[:1500]}")
        print(f"LLM 建议    {verdict.action} (complexity={verdict.complexity_score:.2f})")
        print(f"            {verdict.rationale}")
        print("=" * 68)
        choice = input("采纳(y) / 放弃任务(a) / 继续原样(c) / 挂起(其它): ").strip().lower()
        if choice == "y":
            return HumanRuling(Action(verdict.action), "人采纳 LLM 裁决", verdict.spec_changes)
        if choice == "a":
            return HumanRuling(Action.ABANDON, "人判断该放弃")
        if choice == "c":
            return HumanRuling(Action.CONTINUE, "人判断可原样重试")
        return None


class Architect:
    def __init__(
        self,
        backend: Backend,
        store,
        *,
        policy: Policy,
        human_gate: HumanGate | None = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.policy = policy
        self.human_gate = human_gate
        self.tokens_used = 0
        self._last_soft_consume = time.monotonic()
        # 每个任务试过什么。M2 归因发现架构师**每次都在「第一次见到这个问题」的
        # 状态下决策** —— decide_interrupt 的输入里既没有前几轮的裁决，也没有
        # interrupt_count。这直接解释了实测里的 CONTINUE → CONTINUE → CONTINUE
        # （§11.9b）。这份记录同时喂两个地方：确定性的「决策无效」判据，和模型提示词。
        self._history: dict[str, list[dict]] = {}

    # -- 软信号消费（§3.4）-------------------------------------------------- #

    def should_consume_soft(self, queue_depth: int, step_boundary: bool) -> bool:
        if queue_depth == 0:
            return False
        return (
            step_boundary
            or queue_depth >= self.policy.soft_queue_threshold
            or time.monotonic() - self._last_soft_consume > self.policy.soft_interval_s
        )

    def consume_soft(self, signals: list[Signal]) -> list[Signal]:
        """批量廉价评估，返回需要升级到主模型的那些。"""
        self._last_soft_consume = time.monotonic()
        if not signals:
            return []

        # CONFLICT_SUSPECTED 例外：跨任务冲突是架构师的独有视野，直接升级，
        # 不走廉价评估（§3.4）。
        direct = [s for s in signals if s.type is SignalType.CONFLICT_SUSPECTED]
        rest = [s for s in signals if s.type is not SignalType.CONFLICT_SUSPECTED]

        escalated = list(direct)
        for s in direct:
            s.disposition = Disposition.ESCALATED
            s.consumed_at = time.time()
            self.store.save_signal(s)

        if rest:
            verdicts, tokens = self.backend.triage(rest)
            self.tokens_used += tokens
            by_id = {s.id: s for s in rest}
            for v in verdicts:
                sig = by_id.get(v.signal_id)
                if sig is None:
                    continue
                sig.consumed_at = time.time()
                if v.verdict == "escalate":
                    sig.disposition = Disposition.ESCALATED
                    escalated.append(sig)
                else:
                    sig.disposition = Disposition.IGNORED
                self.store.save_signal(sig)
        return escalated

    # -- 中断决策（§5 / §7）------------------------------------------------- #

    def decide(
        self, state: TaskState, signals: list[Signal], ctx: AgentContext
    ) -> DecisionRecord:
        spec = state.spec

        # HUMAN_INTERVENTION 不走 LLM：人的指令直接成为裁决输入
        human_sig = next(
            (s for s in signals if s.type is SignalType.HUMAN_INTERVENTION), None
        )
        if human_sig is not None:
            instruction = human_sig.payload.get("instruction", "")
            return DecisionRecord(
                task_id=spec.id,
                trigger=[s.id for s in signals],
                decider=Decider.HUMAN,
                action=Action.MODIFY_TASK,
                rationale=f"人在群聊中介入：{instruction}",
                new_spec=spec.bump(goal=instruction or spec.goal),
                resume_mode=ResumeMode.REBASE,
            )

        history = self._history.setdefault(spec.id, [])
        fp = fingerprint(signals)
        streak = 1
        for past in reversed(history):
            if past["fingerprint"] != fp:
                break
            streak += 1

        verdict, tokens = self.backend.decide_interrupt(spec, signals, ctx, history=history)
        self.tokens_used += tokens
        state.tokens_used += tokens

        reason = should_escalate(
            self.policy, spec, state, signals, verdict, identical_streak=streak
        )
        decider = Decider.LLM
        escalation_reason = None
        rationale = verdict.rationale
        action = Action(verdict.action)
        spec_changes = dict(verdict.spec_changes)

        if reason:
            escalation_reason = reason
            if self.human_gate is None:
                # 没有介入入口 -> 挂起，不猜。这是 §7.2「LLM 无权覆盖」的落地。
                return DecisionRecord(
                    task_id=spec.id,
                    trigger=[s.id for s in signals],
                    decider=Decider.LLM,
                    complexity_score=verdict.complexity_score,
                    escalation_reason=reason,
                    action=Action.CONTINUE,
                    rationale=f"需要人决策但无介入入口，任务挂起。{reason}",
                    new_spec=None,
                    resume_mode=None,
                )
            ruling = self.human_gate.review(spec, signals, verdict, reason)
            if ruling is None:
                return DecisionRecord(
                    task_id=spec.id,
                    trigger=[s.id for s in signals],
                    decider=Decider.HUMAN,
                    complexity_score=verdict.complexity_score,
                    escalation_reason=reason,
                    action=Action.CONTINUE,
                    rationale="人未答复，任务挂起等待。",
                    new_spec=None,
                    resume_mode=None,
                )
            decider = Decider.HUMAN
            action = ruling.action
            rationale = ruling.rationale
            spec_changes = ruling.spec_changes or {}

        new_spec = None
        resume_mode = None
        if action is Action.MODIFY_TASK:
            new_spec = self._apply_changes(spec, spec_changes)
            resume_mode = choose_resume_mode(spec, new_spec)
        elif action in (Action.CONTINUE, Action.REASSIGN):
            new_spec = spec
            resume_mode = ResumeMode.RESUME if action is Action.CONTINUE else ResumeMode.RESTART

        history.append(
            {
                "fingerprint": fp,
                "signals": sorted({s.type.value for s in signals}),
                "action": action.value,
                "rationale": rationale[:300],
            }
        )
        return DecisionRecord(
            task_id=spec.id,
            trigger=[s.id for s in signals],
            decider=decider,
            complexity_score=verdict.complexity_score,
            escalation_reason=escalation_reason,
            action=action,
            new_spec=new_spec,
            resume_mode=resume_mode,
            rationale=rationale,
        )

    def _apply_changes(self, spec: TaskSpec, changes: dict) -> TaskSpec:
        kwargs: dict = {}
        if changes.get("goal"):
            kwargs["goal"] = changes["goal"]
        added = changes.get("added_criteria") or []
        if added:
            kwargs["acceptance"] = [
                *spec.acceptance,
                *(
                    Criterion(
                        id=c["id"],
                        description=c["description"],
                        command=c.get("command"),
                    )
                    for c in added
                ),
            ]
        for field_name in ("scope", "token_budget", "max_steps", "deadline_s", "model"):
            if field_name in changes:
                kwargs[field_name] = changes[field_name]
        return spec.bump(**kwargs)

    # -- 验收（§5）---------------------------------------------------------- #

    def verify(self, spec: TaskSpec, ctx: AgentContext) -> tuple[bool, str]:
        """Runtime 已跑完可机器检查的项；这里只判非机器可检的部分。"""
        passed, reason, tokens = self.backend.verify(spec, ctx)
        self.tokens_used += tokens
        return passed, reason

    # -- 拆解复核（§12 M5b）------------------------------------------------- #

    def review_decomposition(self, root_goal: str, specs: list[TaskSpec]) -> DecompositionReview:
        """复核一个拆解：先确定性检查，结构没坏再花 token 问语义。

        风险 #3 的第一个防护。它防的是「架构师是唯一没被验证的环节」里
        **拆解**那一半 —— 中断决策那一半由 §7.2 的确定性下限和 M5a 的停滞判据管。

        必须说清楚的局限：**复核者和拆解者是同一个模型**。这只是「同一个脑子换个
        问法再想一遍」，不是独立复核。真正的独立需要另一个供应商或人（§11.10）。
        """
        issues = deterministic_review(root_goal, specs)
        if any(i.kind in ("empty", "invalid_graph", "no_scope", "no_acceptance") for i in issues):
            # 结构就是坏的，语义复核没有意义，也不该为它花 token
            return DecompositionReview(structural=issues, sufficient=False,
                                       missing=["结构性缺陷未修复，跳过语义复核"], tokens=0)

        sufficient, missing, tokens = self.backend.review_decomposition(root_goal, specs)
        self.tokens_used += tokens
        return DecompositionReview(
            structural=issues, sufficient=sufficient, missing=missing, tokens=tokens
        )

    # -- PROBE 中间探查（§3.2.1）------------------------------------------- #

    def probe(
        self, spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]
    ) -> tuple[bool, str, int]:
        """主动看一眼中间产出。返回 (是否在轨, 理由, 本次 token)。

        这是 `silence_policy=PROBE` 的全部内容：**没有信号不等于没有问题**，
        对 GENERATIVE 类任务「无信号」是常态而非好消息（§3.2.1 的隐蔽失败模式）。
        探查发起方是架构师，不等 Subagent 上报 —— Subagent 压根没有判据可报。
        """
        on_track, reason, tokens = self.backend.probe(spec, ctx, excerpts)
        self.tokens_used += tokens
        return on_track, reason, tokens
