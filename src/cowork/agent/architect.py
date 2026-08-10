"""架构师（§2.3）。

单一实例，持有连续上下文，是唯一的写入决策点。
所有跨任务信息只经它流转；Subagent 之间不通信。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from .. import ids
from ..escalation import deterministic_plan_escalation, should_escalate
from ..llm import ArchitectVerdict, Backend
from ..llm.routing import assign_providers
from ..llm.errors import ModelError
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
    SandboxProfile,
    Signal,
    TaskClass,
    TaskSpec,
    TaskState,
)


def _findings_fingerprint(review: "DecompositionReview") -> str:
    """复核结论的指纹 —— 拆解层的「同样的信号又来了一遍」。

    只取**结论**（结构问题的种类 + 缺口原文），不取子任务 id：换一批 id 重拆
    但复核意见一字不变，恰恰就是「重生成没有改变现实」，指纹必须相同才抓得住。
    """
    payload = "|".join(
        [*sorted(i.kind for i in review.structural), *sorted(review.missing)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class HumanRuling:
    action: Action
    rationale: str
    spec_changes: dict | None = None


@dataclass
class DecompositionReview:
    """拆解复核的结果（§12 M5b / M7 7.1）。

    两半分开放是刻意的：`structural` 免费且不会漏判自己，`missing` 来自模型、
    可能有假阳性也可能有假阴性。**混成一个布尔值会把两种可信度不同的证据抹平。**

    `reviewer` / `independent` 是 M7 7.1 加的：同一个 `sufficient=false`，出自
    拆解者自己还是出自另一个供应商，可信度不是一回事 —— 记录里必须能分开读，
    否则 7.2 的两侧对照无从算起。
    """

    structural: list
    sufficient: bool
    missing: list[str]
    tokens: int = 0
    reviewer: str = ""
    independent: bool = False

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
            "reviewer": self.reviewer,
            "independent": self.independent,
        }


@dataclass(frozen=True)
class SpecTemplate:
    """`SubtaskDraft` → `TaskSpec` 的补全模板（§12 M7 7.3）。

    **模型不填这里的任何字段**。沙箱、工具白名单、各类上限属于隔离边界与成本
    边界，让被隔离方给自己配边界是没有意义的；模型只决定 goal / 验收标准 /
    scope / 依赖 —— 即「做什么」，不是「能用什么做」。
    """

    sandbox: SandboxProfile
    parent_id: str | None = None
    tools: tuple[str, ...] = ("write_file", "read_file", "list_files", "run")
    model: str = "claude-opus-5"
    max_steps: int = 12
    deadline_s: float = 300.0
    token_budget: int = 60_000
    probe_interval_s: float | None = None


@dataclass
class PlanRuling:
    """人对一份拆解的裁决（§12 M7 7.5）。

    `specs` 非空表示人直接给了一份替代拆解 —— 人有写权（§2.4），这是它的体现。
    """

    accept: bool
    rationale: str
    specs: list[TaskSpec] | None = None


@dataclass
class DecompositionResult:
    """一次「生成 → 复核 → 重生成」的终局（§12 M7 7.4）。

    三种终局与执行层同构：ACCEPTED ≙ COMPLETED，AWAITING_HUMAN ≙ AWAITING_HUMAN，
    REJECTED ≙ ABANDONED（人看过了并且不要它）。**都不是异常。**
    """

    status: str                      # ACCEPTED | AWAITING_HUMAN | REJECTED
    specs: list[TaskSpec]
    # 这批子任务共同的 parent_id —— 界面层要用它当那条复合线程的 id（M6）
    root_id: str | None = None
    review: DecompositionReview | None = None
    attempts: int = 0
    tokens: int = 0
    escalation_reason: str | None = None
    decider: str = "LLM"             # LLM | HUMAN
    rationale: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "root_id": self.root_id,
            "attempts": self.attempts,
            "tokens": self.tokens,
            "decider": self.decider,
            "escalation_reason": self.escalation_reason,
            "rationale": self.rationale,
            "specs": [s.to_dict() for s in self.specs],
            "review": self.review.to_dict() if self.review else None,
            "history": self.history,
        }


class HumanGate(Protocol):
    """人的介入入口。返回 None 表示人还没答复 -> 任务停在 AWAITING_HUMAN。

    第三个方法 `assign_models(profiles, providers)`（§10.3.3）是可选的：
    实现了就能在派发前决定每个子任务派给哪家；没实现就全用默认那家。
    **模型选择归人**，架构师只提供任务特点的描述 —— 它在这里是顾问，
    和复核者一样没有决定权。

    拆解层的入口是**另一个方法** `review_plan`（§12 M7 7.5）：它要看的是一份
    拆解和一组 findings，`review()` 的 (spec, signals, verdict) 装不下。
    没实现 `review_plan` 的网关会被当成「拆解层没有人的入口」，拆解因此停在
    AWAITING_HUMAN —— 不猜，同 §7.2「LLM 无权覆盖」。
    """

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

    def review_plan(self, root_goal, specs, review, reason) -> PlanRuling:
        # 拆解层同样是「有人配置了自动放行」：收下当前这份拆解接着跑。
        # 它并没有引入人的判断 —— 生产环境不要用。
        return PlanRuling(
            accept=True,
            rationale=f"[AutoApproveGate] 升级原因：{reason}；直接采纳当前拆解",
        )

    def assign_models(self, profiles, providers) -> dict[str, str]:
        """自动放行 = 不挑，全用默认那家（返回空分配）。

        **这里刻意不替人选**：让「自动」去挑供应商，等于把一个人的决定
        伪装成系统的决定。要挑就用 CliGate 或界面层自己的实现。
        """
        return {}


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

    def review_plan(self, root_goal, specs, review, reason) -> PlanRuling | None:
        print("\n" + "=" * 68)
        print("需要人裁决一份拆解")
        print(f"原始目标    {root_goal}")
        print(f"升级原因    {reason}")
        for s in specs:
            deps = f" ← {', '.join(s.depends_on)}" if s.depends_on else ""
            print(f"  - {s.id}{deps}  scope={s.scope}")
            print(f"    {s.goal}")
            for c in s.acceptance:
                mark = "✓cmd" if c.machine_checkable else "  人判"
                print(f"      [{mark}] {c.description}")
        if review:
            for i in review.structural:
                print(f"结构问题    {i.kind}: {i.detail}")
            for m in review.missing:
                print(f"复核缺口    {m}")
        print("=" * 68)
        choice = input("就按这份拆解跑(y) / 放弃这个目标(a) / 挂起(其它): ").strip().lower()
        if choice == "y":
            return PlanRuling(accept=True, rationale="人确认按当前拆解执行")
        if choice == "a":
            return PlanRuling(accept=False, rationale="人判断这个目标该放弃")
        return None

    def assign_models(self, profiles, providers) -> dict[str, str]:
        """在终端里逐个任务选供应商（§10.3.3）。

        直接回车 = 用默认那家。**默认值是「你配了哪家 key 就用哪家」**，
        所以只有一家可用时这个方法根本不会被调到 —— 没得选就不该问。
        """
        print("\n" + "=" * 68)
        print("给每个子任务挑一家模型（直接回车 = 默认）")
        print(f"可用：{', '.join(providers)}")
        print("=" * 68)
        out: dict[str, str] = {}
        for p in profiles:
            demands = f"  需要：{', '.join(p.demands)}" if p.demands else ""
            print(f"\n[{p.kind}] {p.task_id}\n  {p.summary}{demands}")
            choice = input(f"  用哪家？{'/'.join(providers)} > ").strip().lower()
            if choice in providers:
                out[p.task_id] = choice
            elif choice:
                print(f"  没有 {choice!r} 这家，按默认处理")
        return out


class Architect:
    def __init__(
        self,
        backend: Backend,
        store,
        *,
        policy: Policy,
        human_gate: HumanGate | None = None,
        reviewer_backend: Backend | None = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.policy = policy
        self.human_gate = human_gate
        # 复核者（§12 M7 7.1）。**它没有写权** —— 只在 review_decomposition 里被问一次，
        # 产出 findings，改不了任何 spec。写权仍然只在 decide()/_apply_changes() 这条路上，
        # §2.3 的「唯一写入决策点」因此不变。给它写权 = 两个写入点 = 不变量破了。
        self.reviewer_backend = reviewer_backend
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

        # 挂起时要留下的「系统建议」。下面两条挂起路径的 action/rationale 记的都是
        # **系统的兜底行为**（挂起），不是模型的意见；模型说了什么必须单独存，
        # 否则「等你拍板」的卡片只剩一句升级原因，人看不到系统本来想干什么（M6 §9）。
        suggestion = {
            "action": verdict.action,
            "rationale": verdict.rationale,
            "complexity_score": verdict.complexity_score,
            # 建议里也带上它想改什么 —— 人要判断的往往正是这个
            "spec_changes": dict(verdict.spec_changes),
        }

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
                    suggestion=suggestion,
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
                    suggestion=suggestion,
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
            # 改了哪些字段单独记一份：只有 new_spec 的话，界面层没法说清
            # 「这次新增的是哪条验收标准」，只能把两版 spec 整份摆出来（M6 §9）
            spec_changes=spec_changes,
            # 人接手时也保留模型当时的建议 —— 事后复盘要能对照
            # 「模型想怎么做 / 人最后怎么定的」
            suggestion=suggestion if escalation_reason else None,
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

        `reviewer_backend` 为 None 时复核者就是拆解者自己（M5b 的形态）：
        「同一个脑子换个问法再想一遍」，不是独立复核。给了另一个供应商才谈得上独立
        （§12 M7 7.1）。**独立不等于更准** —— 复核者不共享拆解者的上下文，多出一种
        同模型没有的失败形态：对本来没问题的拆解报缺口。所以 7.2 必须两侧都测。
        """
        issues = deterministic_review(root_goal, specs)
        reviewer = self.reviewer_backend or self.backend
        independent = self.reviewer_backend is not None
        name = getattr(reviewer, "name", "?")
        if any(i.kind in ("empty", "invalid_graph", "no_scope", "no_acceptance") for i in issues):
            # 结构就是坏的，语义复核没有意义，也不该为它花 token
            return DecompositionReview(structural=issues, sufficient=False,
                                       missing=["结构性缺陷未修复，跳过语义复核"], tokens=0,
                                       reviewer="deterministic", independent=independent)

        sufficient, missing, tokens = reviewer.review_decomposition(root_goal, specs)
        self.tokens_used += tokens
        return DecompositionReview(
            structural=issues, sufficient=sufficient, missing=missing, tokens=tokens,
            reviewer=name, independent=independent,
        )

    # -- 拆解生成（§12 M7 7.3 / 7.4）---------------------------------------- #

    def decompose(
        self,
        root_goal: str,
        template: SpecTemplate,
        *,
        feedback: list[str] | None = None,
    ) -> list[TaskSpec]:
        """让模型拆一次，把结果补全成可派发的 TaskSpec。

        风险 #14 的正题：在这之前架构师从来没有真的拆解过任务，
        `Orchestrator` / `Scheduler` 拿到的都是现成的 spec。

        补全在这里做而不是在提示词里要求，是因为 sandbox / tools / 上限
        属于隔离与成本边界 —— 见 `SpecTemplate` 的说明。
        """
        drafts, tokens = self.backend.decompose(root_goal, feedback=feedback)
        self.tokens_used += tokens
        return [self._assemble(d, template) for d in drafts]

    def _assemble(self, draft, template: SpecTemplate) -> TaskSpec:
        task_class = TaskClass(draft.task_class)
        # GENERATIVE 强制 PROBE，而 PROBE 必须有间隔，否则 TaskSpec 直接拒收（§4.1）。
        # 这个间隔不能让模型定 —— 它是成本参数，依据在 policy 里（§11.7）。
        probe_interval = template.probe_interval_s or self.policy.default_probe_interval_s
        return TaskSpec(
            id=draft.id,
            parent_id=template.parent_id,
            goal=draft.goal,
            acceptance=[
                Criterion(id=c["id"], description=c["description"], command=c.get("command"))
                for c in draft.acceptance
            ],
            task_class=task_class,
            sandbox=template.sandbox,
            scope=list(draft.scope),
            depends_on=list(draft.depends_on),
            tools=list(template.tools),
            model=template.model,
            max_steps=template.max_steps,
            deadline_s=template.deadline_s,
            token_budget=template.token_budget,
            probe_interval_s=probe_interval if task_class is TaskClass.GENERATIVE else None,
        )

    def plan(
        self,
        root_goal: str,
        template: SpecTemplate,
        *,
        log: Callable[[str], None] = lambda _m: None,
    ) -> DecompositionResult:
        """生成 → 复核 → 重生成 ≤N 次 → 升级给人（§12 M7 7.4）。

        **和执行层那条循环是同构的**：

            派发 → 验收 → REBASE   → 超 max_rebase     → 升级给人
            生成 → 复核 → 重生成   → 超 max_regenerate → 升级给人

        所以确定性判据复用 `escalation.deterministic_plan_escalation()`，
        上限复用 `policy`，人的入口复用 `HumanGate`（多一个方法）。这里**没有**
        新的中断/恢复机制 —— 有的话就是方向错了。

        写权始终在这个方法手上：复核者只提供 findings，人只能接受或否决，
        **重新拆的动作永远由生成者做**（§2.3 唯一写入决策点）。
        """
        # 这批子任务同属一个根目标，必须有共同的 parent_id。**不给的话三处都坏**：
        #   执行层  parent_id 为空 = 顶层任务，任何 MODIFY_TASK 都无条件升级给人
        #           （§7.2 第 3 条），于是生成出来的任务集失去自我修复能力 ——
        #           而「把失败信号变成更清楚的规格」正是这个系统存在的意义。
        #   界面层  views.thread_list() 按 parent_id 折叠，为空时一次拆解的 N 个
        #           子任务会各占一行，而不是一条复合线程（M6 §10.3）。
        #   时间线  Scheduler.root_id 靠共同 parent_id，为空时分层/复核事件无处可挂。
        # 实测撞到过：4 个子任务里 2 个第一次要改规格就挂起了。
        if template.parent_id is None:
            template = replace(template, parent_id=ids.task_id())

        history: list[dict] = []
        fingerprints: list[str] = []
        specs: list[TaskSpec] = []
        review: DecompositionReview | None = None
        tokens_before = self.tokens_used
        feedback: list[str] | None = None
        attempt = 0
        reason: str | None = None

        while True:
            attempt += 1
            try:
                specs = self.decompose(root_goal, template, feedback=feedback)
            except ModelError as exc:
                # 模型调不动或产出不合规 —— 和执行层同一条路：不抛出去，
                # 变成「需要人」的终局。架构师连拆解都拆不出来时不该猜一个。
                reason = f"生成者无法产出合规拆解：{exc}"
                log(f"[PLAN] 第 {attempt} 轮生成失败：{exc}")
                review = None
                break

            try:
                review = self.review_decomposition(root_goal, specs)
            except ModelError as exc:
                # **复核者失败和生成者失败必须走同一条路**。第一版只接住了生成侧，
                # 于是复核者被截断时整个 plan() 抛穿 —— 手上明明有一份拆解，
                # 却因为「没人复核得了」而崩掉，而不是交给人看一眼（§11.13 实测撞到）。
                reason = f"复核者无法给出结论：{exc}"
                log(f"[PLAN] 第 {attempt} 轮复核失败：{exc}")
                review = None
                break
            fp = _findings_fingerprint(review)
            fingerprints.append(fp)
            history.append(
                {
                    "attempt": attempt,
                    "subtasks": [s.id for s in specs],
                    "fingerprint": fp,
                    "structural": [i.kind for i in review.structural],
                    "missing": list(review.missing),
                    "clean": review.clean,
                }
            )
            log(
                f"[PLAN] 第 {attempt} 轮：{len(specs)} 个子任务，"
                + ("复核通过" if review.clean else f"复核报了 {len(review.missing)} 条缺口")
            )

            if review.clean:
                return DecompositionResult(
                    status="ACCEPTED", specs=specs, root_id=template.parent_id,
                    review=review, attempts=attempt,
                    tokens=self.tokens_used - tokens_before, history=history,
                    rationale="拆解通过结构检查与验收标准反推",
                )

            # attempt = 已经生成过几轮。max_regenerate=2 时总共生成 3 次
            # （1 次初拆 + 2 次重生成），第 3 次仍不过就交给人。
            reason = deterministic_plan_escalation(
                self.policy, attempt=attempt, fingerprints=fingerprints
            )
            if reason:
                break

            # 复核意见喂回给生成者 —— 没有这一步，重生成就是「再抽一次」
            feedback = [
                *(f"{i.kind}: {i.detail}" for i in review.structural),
                *review.missing,
            ]

        return self._escalate_plan(
            root_goal, specs, review, reason or "未知原因",
            attempts=attempt, tokens=self.tokens_used - tokens_before, history=history,
            log=log, root_id=template.parent_id,
        )

    def _escalate_plan(
        self, root_goal: str, specs: list[TaskSpec], review: DecompositionReview | None,
        reason: str, *, attempts: int, tokens: int, history: list[dict],
        log: Callable[[str], None], root_id: str | None = None,
    ) -> DecompositionResult:
        log(f"[PLAN] 升级给人：{reason}")
        gate_review = getattr(self.human_gate, "review_plan", None)
        if gate_review is None:
            # 没有拆解层的入口 -> 挂起，不猜。同 §7.2「LLM 无权覆盖」。
            return DecompositionResult(
                status="AWAITING_HUMAN", specs=specs, root_id=root_id, review=review, attempts=attempts,
                tokens=tokens, escalation_reason=reason, history=history,
                rationale="需要人裁决这份拆解，但没有拆解层的介入入口，挂起。",
            )

        ruling = gate_review(root_goal, specs, review, reason)
        if ruling is None:
            return DecompositionResult(
                status="AWAITING_HUMAN", specs=specs, root_id=root_id, review=review, attempts=attempts,
                tokens=tokens, escalation_reason=reason, decider="HUMAN", history=history,
                rationale="人未答复，拆解挂起等待。",
            )
        if not ruling.accept:
            return DecompositionResult(
                status="REJECTED", specs=specs, root_id=root_id, review=review, attempts=attempts,
                tokens=tokens, escalation_reason=reason, decider="HUMAN", history=history,
                rationale=ruling.rationale,
            )

        final = ruling.specs or specs
        if not final:
            # **空拆解不能被「同意」**。生成者一次都没产出时手上是空列表，
            # 而 AutoApproveGate 对什么都点头 —— 于是「模型调不动」会变成
            # 「ACCEPTED，0 个子任务」，一个失败被记成成功。真实模型第一次跑
            # 就撞上了这个。人要救场只能靠 ruling.specs 自己给一份。
            return DecompositionResult(
                status="AWAITING_HUMAN", specs=[], root_id=root_id, review=review, attempts=attempts,
                tokens=tokens, escalation_reason=reason, decider="HUMAN", history=history,
                rationale=f"{ruling.rationale}（但手上没有任何拆解可以采纳，仍需人给出）",
            )
        return DecompositionResult(
            status="ACCEPTED", specs=final, root_id=root_id, review=review, attempts=attempts,
            tokens=tokens, escalation_reason=reason, decider="HUMAN", history=history,
            rationale=ruling.rationale,
        )

    # -- 按任务选供应商（§10.3.3）------------------------------------------ #

    def assign_models(
        self,
        specs: list[TaskSpec],
        providers: dict[str, str],
        *,
        log: Callable[[str], None] = lambda _m: None,
    ) -> tuple[list[TaskSpec], list]:
        """并行度和分工已经定了，最后一步：每个子任务派给哪家。

        流程是**一次架构师调用 + 一次人的决定**：

            profile_tasks()  架构师描述每个子任务是什么性质的活儿（顾问）
            assign_models()  人按这份描述挑供应商（仲裁）
            assign_providers() 把结果写进 TaskSpec.model，进存储、进界面

        `providers` 是「这台机器上真的能用的家」→ 那家的 Subagent 模型 id。
        **只有一家时直接返回，不问也不花那次调用** —— 没得选的时候提问是在
        浪费人的注意力，而人的注意力是这个系统里最贵的东西（§7.2 的同一条理由）。

        网关没实现 `assign_models` 也直接返回：那表示这个部署里没有「人来挑」
        这个环节，全用默认那家。
        """
        gate_assign = getattr(self.human_gate, "assign_models", None)
        if len(providers) < 2 or gate_assign is None or not specs:
            return list(specs), []

        profiles, tokens = self.backend.profile_tasks(specs)
        self.tokens_used += tokens
        log(f"[MODEL] 架构师描述了 {len(profiles)} 个子任务的性质（{tokens} token）")

        assignment = gate_assign(profiles, sorted(providers)) or {}
        # 人可能只挑了一部分 —— 没挑的保持默认，不替他补
        unknown = {v for v in assignment.values()} - set(providers)
        if unknown:
            log(f"[MODEL] 忽略未知供应商 {sorted(unknown)}")
            assignment = {k: v for k, v in assignment.items() if v in providers}
        for tid, provider in sorted(assignment.items()):
            log(f"[MODEL] {tid} -> {provider}")

        return assign_providers(specs, assignment, providers), profiles

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
