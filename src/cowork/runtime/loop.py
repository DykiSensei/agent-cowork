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
    ) -> StepOutcome:
        spec = ctx.task_spec
        started = time.monotonic()
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
            if step - start_step >= spec.max_steps:
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

                if not result.ok:
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
