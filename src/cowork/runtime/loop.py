"""Step 循环 —— 本设计的控制流核心（§10.1）。

为什么自己写：LangGraph 的 interrupt() 是节点内部的暂停点，不支持从外部抢占
一个正在运行的图。而 §5.1 已规定中断只发生在 step 边界，所以只要 step 循环
是我们自己持有的，「外部抢占」就退化成「不派发下一个 step」——一次状态检查。

这个文件里没有任何 LLM 调用。模型决策由注入的 subagent.next_step() 提供，
Runtime 只负责观测、检测、落盘、拦截。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from .. import ids
from ..actions import AgentAction, Finish, SoftSignalAction, ToolCall
from ..llm.errors import ModelError
from ..signals import SignalSource, SignalType
from ..types import (
    AgentContext,
    Artifact,
    Checkpoint,
    Disposition,
    Signal,
    TaskStatus,
)
from .bus import SignalBus
from .detectors import validate_schema
from .sandbox import Sandbox, ScopeViolation


class StepSource(Protocol):
    """Subagent 需要实现的唯一接口。Runtime 不关心它内部是否用 LLM。"""

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        """返回 (动作, 本步消耗的 token 数)。"""
        ...


@dataclass
class StepOutcome:
    status: TaskStatus
    context: AgentContext
    checkpoint_id: str | None
    steps_run: int
    tokens_used: int
    preempting_signals: list[Signal] = field(default_factory=list)
    output: dict | None = None
    soft_signals: list[Signal] = field(default_factory=list)
    # PROBE 到点：不是中断，是「该让架构师看一眼中间产出了」（§3.2.1）。
    # Runtime 只负责判断「时间到了」这个确定性事实，看不看得懂产出是架构师的事 ——
    # 这条边界就是不变量 2（Runtime 不含 LLM）在 PROBE 上的落地。
    probe_due: bool = False

    @property
    def preempting_signal(self) -> Signal | None:
        return self.preempting_signals[0] if self.preempting_signals else None


class StepLoop:
    def __init__(
        self,
        *,
        bus: SignalBus,
        sandbox: Sandbox,
        store,
        on_checkpoint=None,
    ) -> None:
        self.bus = bus
        self.sandbox = sandbox
        self.store = store
        self.on_checkpoint = on_checkpoint

    # ------------------------------------------------------------------ #

    def run(
        self,
        ctx: AgentContext,
        agent: StepSource,
        *,
        start_step: int = 0,
        tokens_used: int = 0,
        probe_at: float | None = None,
        cycle_steps_used: int = 0,
        cycle_started: float | None = None,
    ) -> StepOutcome:
        """跑到「该停下」为止。

        probe_at 是一个 time.monotonic() 时刻。到点时在 step 边界返回
        probe_due=True，由 Orchestrator 去叫架构师验收中间产出。

        cycle_steps_used / cycle_started 是**同一轮里之前那些探查分段已经用掉的
        预算**。没有它们，每次探查都会把 max_steps 和 deadline_s 的计数清零，
        于是 PROBE 任务变成没有步数上限也没有超时 —— 而 STEP_LIMIT / TIMEOUT
        恰恰是 GENERATIVE 类仅有的几条硬信号（§3.2.1）。
        """
        spec = ctx.task_spec
        started = cycle_started if cycle_started is not None else time.monotonic()
        step = start_step
        tokens = tokens_used
        last_ckpt: str | None = None
        ckpt_step: int | None = None
        output: dict | None = None

        def checkpoint() -> str:
            """在 step 边界落盘。同一 step 内重复调用复用同一个 checkpoint。"""
            nonlocal last_ckpt, ckpt_step
            if last_ckpt is not None and ckpt_step == step:
                return last_ckpt
            cp = Checkpoint(
                id=ids.checkpoint_id(),
                task_id=spec.id,
                step=step,
                agent_context=ctx,
                artifacts=[a.id for a in ctx.produced],
            )
            self.store.save_checkpoint(cp)
            if self.on_checkpoint:
                self.on_checkpoint(cp)
            last_ckpt, ckpt_step = cp.id, step
            return cp.id

        def interrupted(sig: Signal) -> StepOutcome:
            # 抢占队列必须清空：留在队列里的硬信号会在下一轮 step 循环
            # 开头再次触发抢占，把一次中断放大成无限中断。
            drained = self.bus.drain_preempt()
            sigs = drained if any(s is sig for s in drained) else [sig, *drained]
            for s in sigs:
                s.disposition = Disposition.PREEMPTED
                self.store.save_signal(s)
            cid = checkpoint()
            return StepOutcome(
                status=TaskStatus.INTERRUPTED,
                context=ctx,
                checkpoint_id=cid,
                steps_run=step - start_step,
                tokens_used=tokens,
                preempting_signals=sigs,
                soft_signals=self.bus.drain_soft(),
            )

        while True:
            # ---- step 边界：抢占检查。这就是「外部中断」的全部实现 ----
            pending = self.bus.take_preempt()
            if pending is not None:
                return interrupted(pending)

            # ---- 资源类硬信号（对所有 task_class 都可用）----
            if (step - start_step) + cycle_steps_used >= spec.max_steps:
                return interrupted(
                    self.bus.emit_hard(
                        SignalType.STEP_LIMIT, spec.id,
                        payload={"max_steps": spec.max_steps},
                    )
                )
            if time.monotonic() - started > spec.deadline_s:
                return interrupted(
                    self.bus.emit_hard(
                        SignalType.TIMEOUT, spec.id,
                        payload={"deadline_s": spec.deadline_s},
                    )
                )
            if tokens > spec.token_budget:
                return interrupted(
                    self.bus.emit_hard(
                        SignalType.BUDGET_EXCEEDED, spec.id,
                        payload={"used": tokens, "budget": spec.token_budget},
                    )
                )

            # ---- PROBE 到点：让出控制权，但**不是**中断 ----
            # 放在派发下一个 step 之前、资源检查之后：探查看的是「到此为止的产出」，
            # 所以必须先落盘再交出去，否则架构师看到的是上一个 checkpoint。
            #
            # `step > start_step` 是必需的护栏，不是优化：探查在轨后 Orchestrator
            # 会重置计时并再次进来，若此时间隔已过而一个 step 都没派发，就会
            # 「探查 → 无进展 → 再探查」空转烧 token。没有进展就没有可探查的东西。
            if probe_at is not None and step > start_step and time.monotonic() >= probe_at:
                return StepOutcome(
                    status=TaskStatus.RUNNING,
                    context=ctx,
                    checkpoint_id=checkpoint(),
                    steps_run=step - start_step,
                    tokens_used=tokens,
                    soft_signals=self.bus.drain_soft(),
                    probe_due=True,
                )

            # ---- 派发一个 step ----
            step += 1
            try:
                action, cost = agent.next_step(ctx)
            except ModelError as exc:
                # 模型调用失败不该让整个 run 崩 —— 归类成硬信号交给架构师。
                # 代理侧的预算拒绝走的就是这条路（signal_type=BUDGET_EXCEEDED）。
                ctx.reasoning_trace.append(
                    {"role": "runtime", "step": step, "model_error": exc.message[:2000]}
                )
                return interrupted(
                    self.bus.emit_hard(
                        exc.signal_type, spec.id,
                        payload={"origin": "model_call", "status_code": exc.status_code},
                        evidence=exc.message[:4000],
                    )
                )
            tokens += cost
            ctx.reasoning_trace.append(
                {"role": "assistant", "step": step, "action": _describe(action)}
            )

            if isinstance(action, SoftSignalAction):
                # 软信号入队，不中断（§3.3）
                self.bus.emit(
                    Signal(
                        type=SignalType(action.signal_type),
                        task_id=spec.id,
                        source=SignalSource.SUBAGENT,
                        payload={"detail": action.detail},
                    )
                )
                last_ckpt = checkpoint()
                continue

            if isinstance(action, ToolCall):
                try:
                    result = self._exec_tool(action)
                except ScopeViolation as exc:
                    ctx.reasoning_trace.append(
                        {"role": "runtime", "step": step, "error": str(exc)}
                    )
                    last_ckpt = checkpoint()
                    return interrupted(
                        self.bus.emit_hard(
                            SignalType.SCOPE_VIOLATION, spec.id,
                            payload={"resource": exc.resource, "reason": exc.reason},
                            evidence=str(exc),
                        )
                    )

                ctx.reasoning_trace.append(
                    {
                        "role": "tool",
                        "step": step,
                        "name": action.name,
                        "ok": result.ok,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout[-2000:],
                        "stderr": result.stderr[-2000:],
                    }
                )

                if action.name == "write_file" and result.ok:
                    art = Artifact(
                        task_id=spec.id,
                        kind="file",
                        content_ref=action.args["path"],
                        summary=f"step {step} 写入 {action.args['path']}",
                    )
                    self.store.save_artifact(art)
                    ctx.produced.append(art)

                last_ckpt = checkpoint()

                # hard_failure=False 的失败只回给模型，不抢占：
                # 探测一个还不存在的文件是正常的第一步，不是任务级失败（§11.6a）。
                # Subagent 自己预演验收命令失败同理（§11.6e）——
                # 验收的判定权归 Runtime 在 Finish 时行使，预演只是它的自测。
                if (
                    not result.ok
                    and result.hard_failure
                    and not _is_acceptance_rehearsal(spec, action)
                ):
                    return interrupted(
                        self.bus.emit_hard(
                            SignalType.TOOL_FAILURE, spec.id,
                            payload={"tool": action.name, "exit_code": result.exit_code},
                            evidence=(result.stderr or result.stdout)[-4000:],
                        )
                    )
                continue

            if isinstance(action, Finish):
                output = action.output
                ctx.summary = action.summary
                last_ckpt = checkpoint()

                # 产出 schema 校验 -> VALIDATION_FAILED
                errs = validate_schema(output, spec.output_schema)
                if errs:
                    return interrupted(
                        self.bus.emit_hard(
                            SignalType.VALIDATION_FAILED, spec.id,
                            payload={"errors": errs},
                            evidence="\n".join(errs),
                        )
                    )

                # 可机器检查的验收标准 -> TEST_FAILED
                # 验收命令本身也可能撞上沙箱边界（例如它试图写 scope 外的路径）
                try:
                    failed = self._run_acceptance(spec)
                except ScopeViolation as exc:
                    return interrupted(
                        self.bus.emit_hard(
                            SignalType.SCOPE_VIOLATION, spec.id,
                            payload={"resource": exc.resource, "reason": exc.reason,
                                     "during": "acceptance"},
                            evidence=str(exc),
                        )
                    )
                if failed is not None:
                    crit, res = failed
                    return interrupted(
                        self.bus.emit_hard(
                            SignalType.TEST_FAILED, spec.id,
                            payload={
                                "criterion_id": crit.id,
                                "description": crit.description,
                                "exit_code": res.exit_code,
                            },
                            evidence=(res.stdout + res.stderr)[-4000:],
                        )
                    )

                return StepOutcome(
                    status=TaskStatus.COMPLETED,
                    context=ctx,
                    checkpoint_id=last_ckpt,
                    steps_run=step - start_step,
                    tokens_used=tokens,
                    output=output,
                    soft_signals=self.bus.drain_soft(),
                )

            raise TypeError(f"未知动作类型: {action!r}")

    # ------------------------------------------------------------------ #

    def _exec_tool(self, call: ToolCall):
        if call.name == "write_file":
            return self.sandbox.write_file(call.args["path"], call.args["content"])
        if call.name == "read_file":
            return self.sandbox.read_file(call.args["path"])
        if call.name == "list_files":
            return self.sandbox.list_files(call.args.get("path") or ".")
        if call.name == "run":
            return self.sandbox.run(list(call.args["command"]))
        raise ScopeViolation(call.name, "未声明的工具")

    def _run_acceptance(self, spec):
        """跑所有可机器检查的验收标准。返回第一个失败的 (criterion, result)。"""
        for crit in spec.acceptance:
            if not crit.machine_checkable:
                continue
            res = self.sandbox.run(list(crit.command))
            if not res.ok:
                return crit, res
        return None


def _is_acceptance_rehearsal(spec, call: ToolCall) -> bool:
    """Subagent 跑的是不是验收标准里那条命令。

    M2 实测：`run` 非零即抢占占了全部硬信号的 50%，而典型形态是
    「Subagent 自己跑一遍测试 → 失败 → 抢占 → 架构师说『继续』→ Subagent 自己改对」
    ——架构师被叫来一次，什么信息都没提供，花掉约 3.5k token（§11.6e）。

    自测是 step 循环内部的事。真正的验收在 Finish 之后由 Runtime 执行，
    那时失败仍然产生 TEST_FAILED，信号覆盖面一条不少。
    """
    if call.name != "run":
        return False
    argv = [str(x) for x in call.args.get("command") or []]
    return any(list(c.command) == argv for c in spec.acceptance if c.command)


def _describe(action: AgentAction) -> dict:
    if isinstance(action, ToolCall):
        args = dict(action.args)
        if "content" in args:
            args["content"] = f"<{len(args['content'])} chars>"
        return {"kind": "tool_call", "name": action.name, "args": args,
                "thought": action.thought}
    if isinstance(action, Finish):
        return {"kind": "finish", "summary": action.summary, "thought": action.thought}
    return {"kind": "soft_signal", "type": action.signal_type, "detail": action.detail}
