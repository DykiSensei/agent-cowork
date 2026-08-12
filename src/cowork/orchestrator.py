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

from .agent.architect import Architect, HumanGate, HumanRuling
from .agent.subagent import Subagent
from .llm import Backend
from .llm.errors import ModelError
from .policy import DEFAULT_POLICY, Policy
from .resume import apply_resume
from .runtime.bus import SignalBus
from .runtime.loop import StepLoop, StepOutcome
from .runtime.sandbox import Sandbox
from .signals import SignalSource, SignalType
from .types import (
    TERMINAL_STATUSES,
    Action,
    AgentContext,
    Artifact,
    Decider,
    DecisionRecord,
    ResumeMode,
    SilencePolicy,
    Signal,
    TaskEvent,
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
        reviewer_backend: Backend | None = None,
        # None = 用 Architect 的默认（开）。bench 显式传 False：M2/M3 的参数
        # 全部是在没有写入侧复核的情况下测出来的，跑批开着它就不再可比。
        review_writes: bool | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        if spec.sandbox is None:
            # 产出必须落在某个可观测的地方，否则架构师既验收不了也探查不了
            raise ValueError("需要 sandbox：产出要写进 workspace 才能被观测")

        self.spec = spec
        self.backend = backend
        self.store = store
        self.policy = policy
        self.log = log

        self.bus = SignalBus()
        self.sandbox = Sandbox(spec.sandbox, spec.scope)
        # reviewer_backend 传下去才谈得上「独立复核」：不传的话写入侧复核退回
        # 同模型（M5b 的形态）。§11.19 实测同模型也不弱（单臂 deepseek J 0.963），
        # 但换一家仍然更稳妥，所以给调用方这个口子。
        self.architect = Architect(
            backend, store, policy=policy, human_gate=human_gate,
            reviewer_backend=reviewer_backend,
            **({} if review_writes is None else {"review_writes": review_writes}),
        )
        self.loop = StepLoop(bus=self.bus, sandbox=self.sandbox, store=store)

        self.state = TaskState(spec=spec, status=TaskStatus.PENDING)
        self.ctx = AgentContext(task_spec=spec)
        self.decisions: list[DecisionRecord] = []
        # 取消理由。非 None 就是「人已经拍板停下」——_drive 在 step 边界之后
        # 看它，看到了就不再问架构师（见 cancel()）
        self._cancelled: str | None = None
        self._rebase_count = 0
        self._last_probe = time.monotonic()
        self.probe_count = 0
        self.probe_tokens = 0

    # ------------------------------------------------------------------ #

    def inject(self, artifacts: list[Artifact]) -> None:
        """架构师注入只读上下文（§8：传引用不传全文）。"""
        for a in artifacts:
            self.store.save_artifact(a)
        self.ctx.injected.extend(artifacts)

    # -- 时间线（M6 §9 第 4 条）------------------------------------------- #

    def _say(self, msg: str) -> None:
        """日志同时落一条事件。

        `self.log` 仍然是外部可替换的回调（CLI 打屏、界面层收集），但它是**纯文本
        且不持久**的。界面的时间线要「按到达序排列的事件流」，那必须来自存储 ——
        跑完之后再打开界面、或者刷新一次页面，日志就没了。
        """
        self.log(msg)
        self._event("log", text=msg)

    def _event(self, kind: str, *, text: str = "", ref_id: str | None = None,
               payload: dict | None = None) -> None:
        """写一条事件。**写不进去不能影响任务本身**。

        事件是给界面看的旁路，存储层没有这张表（老库、别人实现的 Store）
        不该让一次正常的 run 崩掉。
        """
        append = getattr(self.store, "append_event", None)
        if append is None:
            return
        try:
            append(TaskEvent(task_id=self.spec.id, kind=kind, text=text,
                             ref_id=ref_id, payload=payload or {}))
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, status: TaskStatus) -> None:
        """改状态 + 落库 + 记一条事件。状态迁移是界面最需要的那条线。"""
        self.state.status = status
        self.store.save_task(self.state)
        self._event("status", payload={"status": status.value})

    def intervene(self, instruction: str) -> Signal:
        """人在群聊中介入 -> HUMAN_INTERVENTION 硬信号 -> 下个 step 边界立即抢占。"""
        sig = self.bus.human_intervention(self.spec.id, instruction)
        self.store.save_signal(sig)
        self.log(f"[HUMAN] 介入: {instruction}")
        # human 与 log 分开：界面要把人说的话渲染成对话气泡，不是一行日志
        self._event("human", text=instruction, ref_id=sig.id)
        return sig

    def cancel(self, reason: str = "") -> Signal:
        """人要求停下来。**这是 intervene 的另一半，不是它的变体。**

        `intervene` 说的是「换个做法接着干」，所以它把控制权交回架构师；
        cancel 说的是「别干了」，架构师无事可决 —— 走完抢占之后直接进 ABANDONED，
        **不调 decide()**。省下的不只是一次 3.5k token 的调用：让架构师去裁决
        一件人已经拍板的事，它有可能回 CONTINUE，那时候「取消」就成了一个建议。

        停下来的时机仍然是 step 边界（`take_preempt()`），§10.1 的地基不动 ——
        所以最坏要等当前 step 跑完，实测中位 1.65s / p95 3.11s。
        """
        self._cancelled = reason.strip() or "人取消了这个任务"
        sig = self.bus.human_intervention(self.spec.id, f"[取消] {self._cancelled}")
        self.store.save_signal(sig)
        self.log(f"[HUMAN] 取消: {self._cancelled}")
        self._event("human", text=f"取消任务：{self._cancelled}", ref_id=sig.id)
        return sig

    def _no_decider(self, exc: ModelError) -> RunResult:
        """架构师侧的模型调用失败 —— 挂起等人，不把异常抛出去。

        **架构师的每一次模型调用都要走这里**：中断决策、验收、探查、软信号分诊。
        原来只有 `decide()` 被接住，于是「跑完了但验收调用失败」「探查到点但
        分诊调用失败」会以 traceback 收尾，而库里的状态停在 RUNNING ——
        服务层的 cancel 回 409（不在活任务注册表）、ruling 也回 409
        （不是 AWAITING_HUMAN），那条线程从界面上再也动不了。

        日志写在状态迁移**之前**：AWAITING_HUMAN 是终局态，它之后不该再有事件
        （界面把「最后一条状态事件是不是 AWAITING_HUMAN」当作要不要给人出裁决
        表单的判据）。
        """
        self._say(
            f"[STOP] 架构师无法决策（{exc.signal_type.value}）: {exc.message[:200]}"
        )
        self._set_status(TaskStatus.AWAITING_HUMAN)
        return RunResult(self.state, self.ctx, self.decisions)

    def _finish_cancelled(self, triggers: list[Signal]) -> RunResult:
        """取消的收尾：记一条 decider=HUMAN 的裁决，然后 ABANDONED。

        为什么还要写裁决记录：ABANDONED 在界面上和「架构师主动放弃」是同一个
        终局，不写的话时间线上只剩一个没有来由的终止。这条记录不带 new_spec ——
        取消不改 spec，所以它不构成第二个写入点（§2.3）。
        """
        record = DecisionRecord(
            task_id=self.spec.id,
            trigger=[s.id for s in triggers],
            decider=Decider.HUMAN,
            action=Action.ABANDON,
            rationale=self._cancelled or "人取消了这个任务",
        )
        self.store.save_decision(record)
        self.decisions.append(record)
        self._event("decision", ref_id=record.id, payload={"action": record.action.value})
        self._set_status(TaskStatus.ABANDONED)
        self._say(f"[STOP] 人取消了这个任务：{record.rationale}")
        return RunResult(self.state, self.ctx, self.decisions)

    # ------------------------------------------------------------------ #

    def run(self, max_cycles: int = 8) -> RunResult:
        self.state.started_at = time.time()
        self._last_probe = time.monotonic()
        self.store.save_task(self.state)
        return self._drive(max_cycles)

    # ------------------------------------------------------------------ #
    # restore 路径（M6 §9）：人答复之后，从存储把任务重建出来接着跑
    # ------------------------------------------------------------------ #

    @classmethod
    def restore(
        cls,
        task_id: str,
        *,
        backend: Backend,
        store,
        policy: Policy = DEFAULT_POLICY,
        human_gate: HumanGate | None = None,
        reviewer_backend: Backend | None = None,
        review_writes: bool | None = None,
        log: Callable[[str], None] = print,
    ) -> "Orchestrator":
        """从存储重建一个挂起的任务。

        现场材料全在存储里：TaskState 在 tasks 表，AgentContext 在 checkpoint 里
        （produced / reasoning_trace 两个顶层键，from_dict 原样回来），rebase
        次数与架构师的指纹历史从 decisions / signals 表重算 —— 不加新存储。
        """
        state = store.load_task(task_id)
        if state is None:
            raise ValueError(f"没有这个任务: {task_id}")
        # AWAITING_HUMAN → 人的裁决（`resume_with_ruling`）；
        # 终局三种 → 人的追加要求（`follow_up`，M12）。
        # **RUNNING / INTERRUPTED / PENDING 不许 restore**：它们要么正在跑、
        # 要么还在飞行中，重建一份现场等于同一个任务跑两遍。
        if (
            state.status is not TaskStatus.AWAITING_HUMAN
            and state.status not in TERMINAL_STATUSES
        ):
            raise ValueError(
                f"只有挂起或已终局的任务能 restore（当前 {state.status.value}）"
            )
        orch = cls(
            state.spec,
            backend=backend,
            store=store,
            policy=policy,
            human_gate=human_gate,
            reviewer_backend=reviewer_backend,
            review_writes=review_writes,
            log=log,
        )
        orch.state = state
        cp = (
            store.load_checkpoint(state.checkpoint_id)
            if state.checkpoint_id
            else None
        )
        orch.ctx = (
            cp.agent_context if cp is not None else AgentContext(task_spec=state.spec)
        )
        decisions = store.decisions_for(task_id)
        orch.decisions = list(decisions)
        orch._rebase_count = sum(
            1 for d in decisions if d.resume_mode is ResumeMode.REBASE
        )
        orch.architect.prime_history(task_id, store)
        return orch

    def resume_with_ruling(
        self, ruling: HumanRuling, max_cycles: int = 8
    ) -> RunResult:
        """人的答复到了：先落成裁决（走架构师，不绕过去），再接着跑。

        max_cycles 是**恢复之后**的新预算 —— 挂起前已经烧掉的 cycle 不清零，
        interrupt_count / rebase 次数都在 state 里带着。
        """
        decision = self.architect.apply_human_ruling(self.state, ruling, self.store)
        self.store.save_decision(decision)
        self.decisions.append(decision)
        self._event(
            "decision", ref_id=decision.id, payload={"action": decision.action.value}
        )
        self._render(decision)
        return self._drive(max_cycles, first=decision)

    def follow_up(self, instruction: str, max_cycles: int = 8) -> RunResult:
        """任务已经收尾了，人还有话说 —— **原地续跑**（M12，§11.30）。

        「一次做不到位、要改几轮」是真实使用里最常见的形状，而这套系统原来在
        终局那一刻就把门关死了：`intervene` 只对活着的循环有效（要有人来取抢占
        队列），`ruling` 只认 AWAITING_HUMAN。人手上什么都没有，只能重发一个
        任务，而那会**丢掉已经做出来的产物和整条执行记录**。

        **没有新机制**。人的追加要求走的就是 `intervene` 那条路：

            人说的话 → HUMAN_INTERVENTION 硬信号 → 架构师落成裁决
                     （`apply_follow_up`）→ 既有的 apply_resume → 既有的 cycle 循环

        三个后果是刻意的：

        - **写权不变**（§2.3）。人提供的是**信号**，新 spec 由架构师构造。
          和中途介入一样不问模型 —— 人是这条裁决的 decider，问模型只是多花
          一次钱去猜人的意思。
        - **状态从终局回到 RUNNING**，`revision` +1。M6 契约因此改了：
          「终局」不再等于「这条线程永远不会再动」，只等于「此刻它自己不会动」。
        - **产物保留、执行记录压成摘要**（`choose_resume_mode` 会选 REBASE：
          goal 变了但 scope 有交集）。产物留着正是原地续跑相对「重发一个任务」
          的全部价值；trace 不留是 REBASE 本来的意义 —— 旧目标的推理痕迹会
          把下一轮带偏（§6）。

        取消（`cancel`）不走这里：那条路人已经拍板，架构师无事可决。
        """
        was = self.state.status
        if was not in TERMINAL_STATUSES:
            raise ValueError(
                f"只有已终局的任务能追加要求（当前 {was.value}）；"
                "还在跑的用 intervene，挂起的用 ruling"
            )
        sig = self.intervene(instruction)
        # **抢占队列必须清空**：`intervene` 把信号也塞进了抢占队列，而此刻
        # 没有任何 step 循环会来取它 —— 留着的话，续跑的第一步刚开头就会被它
        # 抢占一次，白烧一轮架构师（同「一次中断放大成无限中断」那条坑）。
        self.bus.drain_preempt()
        self.state.interrupt_count += 1
        self.state.signal_log.append(sig.id)
        self._event("signal", ref_id=sig.id, payload={"type": sig.type.value})
        self._set_status(TaskStatus.INTERRUPTED)
        self._say(f"[HUMAN] 追加要求，任务从 {was.value} 续跑")

        # 走架构师，但**不问模型**：人是这条裁决的 decider（同 `apply_human_ruling`
        # 和 `decide()` 里那条 HUMAN_INTERVENTION 快路径）。写权仍然在架构师手上
        # —— 新 spec 是它构造的，人给的只有那句话。
        decision = self.architect.apply_follow_up(self.state, sig)
        self.store.save_decision(decision)
        self.decisions.append(decision)
        self._event("decision", ref_id=decision.id,
                    payload={"action": decision.action.value})
        self._render(decision)
        return self._drive(max_cycles, first=decision)

    # ------------------------------------------------------------------ #

    def _drive(
        self, max_cycles: int, first: DecisionRecord | None = None
    ) -> RunResult:
        """cycle 循环主体。`first` 是 restore 路径带进来的裁决：已经落库、
        记过事件，这里直接应用（不先跑一轮 step 循环）。
        """
        decision = first
        for cycle in range(1, max_cycles + 1):
            # 取消可能在两个 cycle 之间到达（上一轮刚收尾、下一轮还没起 Subagent）。
            # 这里和下面 INTERRUPTED 之后各查一次，两处合起来才没有缝。
            if self._cancelled is not None:
                return self._finish_cancelled([])
            if decision is None:
                self.state.status = TaskStatus.RUNNING
                subagent = Subagent(self.backend)
                self.state.agent_id = subagent.id
                self.store.save_task(self.state)
                self._say(
                    f"[RUN ] cycle={cycle} rev={self.ctx.task_spec.revision} "
                    f"agent={subagent.id} step={self.state.current_step}"
                )

                try:
                    outcome, probe_sig = self._run_with_probes(subagent)
                except ModelError as exc:
                    # 探查也是模型调用（§3.2.1），它失败同样不能把 run 打挂
                    return self._no_decider(exc)

                # 软信号在检查点批量消费（§3.4）
                if outcome.soft_signals:
                    for s in outcome.soft_signals:
                        self.store.save_signal(s)
                    try:
                        escalated = self.architect.consume_soft(outcome.soft_signals)
                    except ModelError as exc:
                        # 分诊走的是廉价模型，但它照样会撞上耗尽的 key
                        return self._no_decider(exc)
                    self._say(
                        f"[SOFT] 消费 {len(outcome.soft_signals)} 条，"
                        f"升级 {len(escalated)} 条"
                    )

                if probe_sig is not None:
                    # 探查发现跑偏。走的是和「架构师验收不通过」完全相同的路径。
                    triggers = [probe_sig]
                elif outcome.status is TaskStatus.COMPLETED:
                    try:
                        passed, reason = self.architect.verify(
                            self.ctx.task_spec, self.ctx
                        )
                    except ModelError as exc:
                        # 验收是**每次 COMPLETED 都会走**的一次模型调用。
                        # 它抛出去的话，一个已经做完的任务会以 traceback 收尾，
                        # 而库里停在 RUNNING —— 界面上既停不掉也裁决不了。
                        return self._no_decider(exc)
                    if passed:
                        self._set_status(TaskStatus.COMPLETED)
                        self._say(f"[DONE] {reason}")
                        return RunResult(self.state, self.ctx, self.decisions, outcome.output)

                    # 验收不通过 -> 当作 L0 信号处理（§5 流程图右下角）
                    self._say(f"[FAIL] 架构师验收不通过: {reason}")
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
                self.state.interrupt_count += 1
                self.state.signal_log.extend(s.id for s in triggers)
                self._set_status(TaskStatus.INTERRUPTED)
                for s in triggers:
                    self._event("signal", ref_id=s.id, payload={"type": s.type.value})
                self._say(
                    f"[STOP] {triggers[0].type.value if triggers else '未知'} "
                    f"@step={self.state.current_step} interrupt_count={self.state.interrupt_count}"
                )

                # 人已经拍板停下：不问架构师（它可能回 CONTINUE，那取消就成了建议）
                if self._cancelled is not None:
                    return self._finish_cancelled(triggers)

                try:
                    decision = self.architect.decide(self.state, triggers, self.ctx)
                except ModelError as exc:
                    # 架构师自己也调不动模型了（典型场景：virtual key 预算耗尽，
                    # Subagent 和架构师用同一把 key）。没有决策者，只能挂起等人。
                    return self._no_decider(exc)

                self.store.save_decision(decision)
                self.decisions.append(decision)
                self._event("decision", ref_id=decision.id,
                            payload={"action": decision.action.value})
                self._render(decision)

            # ---- 应用裁决（restore 带进来的那一轮直接落在这里） ----
            if decision.action is Action.ABANDON:
                self._set_status(TaskStatus.ABANDONED)
                return RunResult(self.state, self.ctx, self.decisions)

            if decision.resume_mode is None or decision.new_spec is None:
                self._set_status(TaskStatus.AWAITING_HUMAN)
                return RunResult(self.state, self.ctx, self.decisions)

            if decision.resume_mode is ResumeMode.REBASE:
                self._rebase_count += 1
                if self._rebase_count > self.policy.max_rebase:
                    # 风险 #5：多次 REBASE 后摘要压缩会累积失真。
                    # 日志在状态迁移之前 —— 理由同 `_no_decider()`
                    self._say(
                        f"[STOP] REBASE 次数超过上限 {self.policy.max_rebase}，挂起等人"
                    )
                    self._set_status(TaskStatus.AWAITING_HUMAN)
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
            decision = None

        self._set_status(TaskStatus.FAILED)
        self._say(f"[STOP] 超过 max_cycles={max_cycles}")
        return RunResult(self.state, self.ctx, self.decisions)

    # ------------------------------------------------------------------ #
    # PROBE（§3.2.1）
    # ------------------------------------------------------------------ #

    def _run_with_probes(self, subagent) -> tuple[StepOutcome, Signal | None]:
        """跑一段，途中把 PROBE 探查消化掉。

        **探查不是中断**，所以它不消耗一个 cycle、不换 Subagent、不动 revision ——
        在轨就直接接着跑。只有探查判定跑偏时才升级成信号，走既有的中断链路。

        `silence_policy=TRUST` 时 `_probe_at()` 返回 None，整段退化成一次
        `loop.run()`，与 M2 之前的行为逐字相同。
        """
        soft: list[Signal] = []
        # 一轮的步数与时间预算跨探查分段累计，不能每段清零（见 loop.run 的注释）
        cycle_steps = 0
        cycle_started = time.monotonic()
        while True:
            outcome = self.loop.run(
                self.ctx,
                subagent,
                start_step=self.state.current_step,
                tokens_used=self.state.tokens_used,
                probe_at=self._probe_at(),
                cycle_steps_used=cycle_steps,
                cycle_started=cycle_started,
            )
            cycle_steps += outcome.steps_run
            self.ctx = outcome.context
            self.state.current_step += outcome.steps_run
            self.state.tokens_used = outcome.tokens_used
            self.state.checkpoint_id = outcome.checkpoint_id
            self.state.artifacts = [a.id for a in self.ctx.produced]
            soft.extend(outcome.soft_signals)
            outcome.soft_signals = soft

            if not outcome.probe_due:
                return outcome, None

            on_track, reason, tokens = self.architect.probe(
                self.ctx.task_spec, self.ctx, self._produced_excerpts()
            )
            self.state.tokens_used += tokens
            self.probe_count += 1
            self.probe_tokens += tokens
            self._last_probe = time.monotonic()
            self._say(
                f"[PROBE] #{self.probe_count} @step={self.state.current_step} "
                f"{'在轨' if on_track else '跑偏'}: {reason[:80]}"
            )
            if on_track:
                continue

            # 跑偏 -> 当作 L0 信号处理，与「架构师验收不通过」同构。
            # 注意信号是 Orchestrator 发的而不是 Runtime 发的：判定来自模型，
            # 不是确定性观测。用 payload.origin 把这个区别留在数据里。
            sig = self.bus.emit_hard(
                SignalType.VALIDATION_FAILED,
                self.spec.id,
                payload={"origin": "architect_probe", "probe_index": self.probe_count},
                evidence=reason,
            )
            self.store.save_signal(sig)
            return outcome, sig

    def _probe_at(self) -> float | None:
        if self.ctx.task_spec.silence_policy is not SilencePolicy.PROBE:
            return None
        interval = (
            self.ctx.task_spec.probe_interval_s or self.policy.default_probe_interval_s
        )
        return self._last_probe + interval

    def _produced_excerpts(self) -> dict[str, str]:
        """把产出内容读出来给架构师看。

        读文件是确定性操作，归 Runtime 的 sandbox —— 架构师没有也不该有
        文件系统访问权。同一路径被写多次时天然去重（dict 按路径收敛）。
        """
        cap = self.policy.probe_excerpt_chars
        out: dict[str, str] = {}
        for a in self.ctx.produced:
            res = self.sandbox.read_file(a.content_ref)
            if res.ok:
                out[a.content_ref] = res.stdout[:cap]
        return out

    # ------------------------------------------------------------------ #

    def _render(self, d: DecisionRecord) -> None:
        """§7.3：每条 DecisionRecord 都渲染出来，LLM 决策与人的决策同等展示。"""
        self._say(
            f"[DEC ] decider={d.decider.value} action={d.action.value} "
            f"resume={d.resume_mode.value if d.resume_mode else '-'} "
            f"complexity={d.complexity_score if d.complexity_score is not None else '-'}"
        )
        if d.escalation_reason:
            self._say(f"       升级原因: {d.escalation_reason}")
        self._say(f"       理由: {d.rationale}")
        if d.new_spec:
            self._say(f"       新 spec: rev={d.new_spec.revision} goal={d.new_spec.goal[:60]}")
