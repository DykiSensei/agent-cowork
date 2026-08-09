"""复合任务调度（§12 M4）。

从单任务扩展到多任务并行，同时守住 §1.4 第一条约束：**执行层中心化，
Subagent 之间不允许直接通信**。这里的并行是「多个 Orchestrator 同时跑」，
每个 Orchestrator 仍然只和架构师说话 —— 并行度加在调度层，不是通信层。
新增的 API 面必须是「调度器 → 任务」的，不能是「任务 ↔ 任务」的。

三件事：

  分层    plan.build_plan()，确定性，无 LLM
  并行    每层内 ThreadPoolExecutor，每个任务一个独立 Orchestrator
  冲突    产出层的确定性检查 + 架构师仲裁

**冲突检测为什么不能只靠软信号**：§3.1 已经确立软信号靠不住，而并行写同一个
文件的后果是「谁后写谁赢」且**完全静默** —— 没有任何异常、没有非零退出码，
产出就那么没了。所以冲突必须是确定性检出的 L0（`CONFLICT_DETECTED`）。

**仲裁为什么不新开一条决策通道**：架构师已经是唯一的写入决策点（§2.3）。
冲突被表达成一条硬信号后，走的就是既有的 `Architect.decide()` —— 它本来就会
MODIFY_TASK（改窄 scope）/ REASSIGN / ABANDON。为冲突单开一套裁决逻辑，
等于承认「唯一决策点」不成立。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent.architect import Architect, HumanGate
from .llm import Backend
from .orchestrator import Orchestrator, RunResult
from .plan import Plan, build_plan
from .policy import DEFAULT_POLICY, Policy
from .runtime.bus import SignalBus
from .signals import SignalType
from .types import Signal, TaskSpec, TaskStatus


@dataclass
class CompositeResult:
    plan: Plan
    results: dict[str, RunResult] = field(default_factory=dict)
    conflicts: list[Signal] = field(default_factory=list)
    arbitrations: list[dict] = field(default_factory=list)
    review: Any = None
    wall_seconds: float = 0.0

    @property
    def completed(self) -> bool:
        return bool(self.results) and all(
            r.state.status is TaskStatus.COMPLETED for r in self.results.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "completed": self.completed,
            "wall_seconds": round(self.wall_seconds, 2),
            "tasks": {
                tid: {
                    "status": r.state.status.value,
                    "revision": r.state.spec.revision,
                    "steps": r.state.current_step,
                    "interrupts": r.state.interrupt_count,
                    "tokens": r.state.tokens_used,
                    "artifacts": [a.content_ref for a in r.context.produced],
                }
                for tid, r in self.results.items()
            },
            "conflicts": [s.to_dict() for s in self.conflicts],
            "arbitrations": self.arbitrations,
            "review": self.review.to_dict() if self.review else None,
        }


class Scheduler:
    def __init__(
        self,
        specs: list[TaskSpec],
        *,
        backend: Backend,
        store,
        policy: Policy = DEFAULT_POLICY,
        human_gate: HumanGate | None = None,
        max_parallel: int = 4,
        root_goal: str | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.specs = list(specs)
        # 有原始目标才谈得上「拆解复核」—— 复核问的是「这些子任务合起来
        # 等不等于它」。没给就跳过语义复核，只做结构检查（§12 M5b）。
        self.root_goal = root_goal
        self.review: "DecompositionReview | None" = None
        self.backend = backend
        self.store = store
        self.policy = policy
        self.human_gate = human_gate
        self.max_parallel = max_parallel
        self.log = log
        self.plan = build_plan(self.specs)
        self.bus = SignalBus()
        # 仲裁用的架构师是**同一个实例语义**：跨任务信息只经它流转（§2.3）。
        # 各任务的 Orchestrator 内部各有自己的 Architect 做本任务决策，
        # 但冲突这种跨任务视野只能在这一层看到。
        self.architect = Architect(backend, store, policy=policy, human_gate=human_gate)

    # ------------------------------------------------------------------ #

    def run(self, *, max_cycles: int = 8) -> CompositeResult:
        started = time.monotonic()
        result = CompositeResult(plan=self.plan)

        for issue in self.plan.issues:
            self.log(f"[PLAN] {issue.kind}: {issue.detail} {list(issue.tasks)}")

        # 拆解复核放在**派发之前**：拆错了就不该开跑，跑完再发现就白烧了。
        if self.root_goal:
            self.review = self.architect.review_decomposition(self.root_goal, self.specs)
            result.review = self.review
            for i in self.review.structural:
                self.log(f"[REVIEW] 结构 {i.kind}: {i.detail} {list(i.tasks)}")
            if self.review.sufficient:
                self.log("[REVIEW] 验收标准反推：覆盖完整")
            else:
                for m in self.review.missing:
                    self.log(f"[REVIEW] 可能遗漏: {m}")

        for i, layer in enumerate(self.plan.layers, start=1):
            self.log(
                f"[LAYER] {i}/{len(self.plan.layers)} "
                f"并行 {len(layer)}: {[t.id for t in layer]}"
            )
            self._run_layer(layer, result, max_cycles=max_cycles)

            # 冲突检测放在**层与层之间**：同层任务已经全部落盘，产出集合是稳定的。
            # 放在层内会读到还在写的中间态。
            conflicts = self._detect_conflicts([t.id for t in layer], result)
            if conflicts:
                result.conflicts.extend(conflicts)
                self._arbitrate(conflicts, result)

        result.wall_seconds = time.monotonic() - started
        return result

    # ------------------------------------------------------------------ #

    def _run_layer(self, layer: list[TaskSpec], result: CompositeResult, *, max_cycles: int):
        if len(layer) == 1:
            tid, run = self._run_one(layer[0], max_cycles, result)
            result.results[tid] = run
            return

        workers = min(self.max_parallel, len(layer))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._run_one, spec, max_cycles, result) for spec in layer]
            for fut in futures:
                tid, run = fut.result()
                result.results[tid] = run

    def _run_one(
        self, spec: TaskSpec, max_cycles: int, result: CompositeResult
    ) -> tuple[str, RunResult]:
        orch = Orchestrator(
            spec,
            backend=self.backend,
            store=self.store,
            policy=self.policy,
            human_gate=self.human_gate,
            log=lambda m, _t=spec.id: self.log(f"  [{_t}] {m}"),
        )
        # 上游产出以只读上下文注入 —— 传引用不传全文（§8）。
        # 这是下游任务能看到上游成果的**唯一**途径：经调度层注入，
        # 不是 Subagent 之间直连（§1.4 第一条）。上游任务只可能在更早的层，
        # 已经跑完，所以直接从内存里的结果拿，不用回查存储。
        upstream = [
            a
            for dep in spec.depends_on
            if dep in result.results
            for a in result.results[dep].context.produced
        ]
        if upstream:
            orch.inject(upstream)
        return spec.id, orch.run(max_cycles=max_cycles)

    # ------------------------------------------------------------------ #
    # 4.3 冲突检测：产出层的确定性检查
    # ------------------------------------------------------------------ #

    def _detect_conflicts(
        self, layer_ids: list[str], result: CompositeResult
    ) -> list[Signal]:
        """**同一层内**两个任务写了同一份产出 = 冲突。

        「同一层」这个限定是本质的，不是优化：跨层写同一个文件是**有序的交接**
        （下游在上游产出上继续做），那是拆解的正常形态，不是冲突。只有并行写
        才会「谁后写谁赢」，而且完全静默。

        比对的是实际写出来的 `content_ref`，不是声明的 scope —— 声明层面的交集在
        `plan.build_plan()` 里已经被静态拦掉并串行化了。这里抓的是静态检查**看不到**
        的那种：架构师在运行中用 MODIFY_TASK 改了 scope，把两个并行任务撞到一起。
        """
        seen: dict[str, list[str]] = {}
        for tid in layer_ids:
            run = result.results.get(tid)
            if run is None:
                continue
            for art in run.context.produced:
                seen.setdefault(art.content_ref, [])
                if tid not in seen[art.content_ref]:
                    seen[art.content_ref].append(tid)

        already = {
            (s.payload.get("resource"), tuple(s.payload.get("tasks", [])))
            for s in result.conflicts
        }
        out: list[Signal] = []
        for resource, tids in sorted(seen.items()):
            if len(tids) < 2:
                continue
            key = (resource, tuple(sorted(tids)))
            if key in already:
                continue
            # 归属给**后完成**的那个任务：它是覆盖方，决策该落在它头上。
            # 顺序取自 store 里 artifact 的 created_at，确定性。
            owner = self._later_writer(resource, tids, result)
            sig = self.bus.emit_hard(
                SignalType.CONFLICT_DETECTED,
                owner,
                payload={"resource": resource, "tasks": sorted(tids), "owner": owner},
                evidence=f"产出 {resource} 被 {len(tids)} 个任务写过: {sorted(tids)}",
            )
            self.store.save_signal(sig)
            self.log(f"[CONFLICT] {resource} <- {sorted(tids)}（归属 {owner}）")
            out.append(sig)
        return out

    def _later_writer(self, resource: str, tids: list[str], result: CompositeResult) -> str:
        latest, owner = -1.0, tids[-1]
        for tid in tids:
            for art in result.results[tid].context.produced:
                if art.content_ref == resource and art.created_at > latest:
                    latest, owner = art.created_at, tid
        return owner

    # ------------------------------------------------------------------ #
    # 4.4 仲裁：不新开决策通道，走既有的 Architect.decide()
    # ------------------------------------------------------------------ #

    def _arbitrate(self, conflicts: list[Signal], result: CompositeResult) -> None:
        for sig in conflicts:
            owner = sig.payload["owner"]
            run = result.results.get(owner)
            if run is None:
                continue
            state = run.state
            state.interrupt_count += 1
            state.signal_log.append(sig.id)
            decision = self.architect.decide(state, [sig], run.context)
            self.store.save_decision(decision)
            run.decisions.append(decision)
            self.arbitration_log(sig, decision, result)

    def arbitration_log(self, sig: Signal, decision, result: CompositeResult) -> None:
        self.log(
            f"[ARBIT] {sig.payload['resource']} -> {decision.action.value}"
            f"（decider={decision.decider.value}）: {decision.rationale[:80]}"
        )
        result.arbitrations.append(
            {
                "resource": sig.payload["resource"],
                "tasks": sig.payload["tasks"],
                "owner": sig.payload["owner"],
                "action": decision.action.value,
                "decider": decision.decider.value,
                "escalation_reason": decision.escalation_reason,
                "rationale": decision.rationale,
            }
        )
