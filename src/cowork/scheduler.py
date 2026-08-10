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
from .types import Signal, TaskEvent, TaskSpec, TaskStatus


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
            # 每个任务摊开 TaskState.to_dict()，与单任务的 --json 用同一套字段名 ——
            # 同一个东西两处叫法不同，界面层就要写两套解析（M6-界面层接口.md）。
            # `produced` 单独一个键：state 里的 `artifacts` 是 id，这里要的是路径，
            # 两者不是一回事，不能覆盖掉。
            "tasks": {
                tid: {
                    **r.state.to_dict(),
                    "produced": [a.content_ref for a in r.context.produced],
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
        reviewer_backend: Backend | None = None,
        log: Callable[[str], None] = print,
        registry: dict | None = None,
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
        # 活 Orchestrator 注册表（task_id → Orchestrator）：服务层用它找
        # 正在跑的任务做人介入。不需要介入入口的调用方不传。
        self.registry = registry
        self.plan = build_plan(self.specs)
        # 复合任务在界面上是**一条线程**，而它的时间线（分层、复核、冲突）不属于
        # 任何一个子任务。子任务共同的 parent_id 就是那条线程的 id；
        # 没有共同父 id 时（一组互不相干的 spec）就没有这条线程，事件也无处可挂。
        roots = {s.parent_id for s in self.specs if s.parent_id}
        self.root_id = roots.pop() if len(roots) == 1 else None
        self.bus = SignalBus()
        # 仲裁用的架构师是**同一个实例语义**：跨任务信息只经它流转（§2.3）。
        # 各任务的 Orchestrator 内部各有自己的 Architect 做本任务决策，
        # 但冲突这种跨任务视野只能在这一层看到。
        # reviewer_backend 只影响拆解复核那一次调用：仲裁、中断决策仍然走 backend。
        # 复核者是顾问，不参与任何写入（§12 M7 的角色表）。
        self.architect = Architect(
            backend, store, policy=policy, human_gate=human_gate,
            reviewer_backend=reviewer_backend,
        )

    # -- 时间线（M6 §9 第 4 条）------------------------------------------- #

    def _say(self, msg: str) -> None:
        """调度器自己的日志也要落成事件，挂在复合线程上。

        子任务的事件由各自的 Orchestrator 写，这里写的是**层与层之间**发生的事：
        分层结果、拆解复核、冲突仲裁 —— 那些在任何一个子任务里都看不到。
        """
        self.log(msg)
        self._event("log", text=msg)

    def _event(self, kind: str, *, text: str = "", ref_id: str | None = None,
               payload: dict | None = None) -> None:
        append = getattr(self.store, "append_event", None)
        if append is None or self.root_id is None:
            return
        try:
            append(TaskEvent(task_id=self.root_id, kind=kind, text=text,
                             ref_id=ref_id, payload=payload or {}))
        except Exception:  # noqa: BLE001 - 事件是旁路，写不进去不该影响调度
            pass

    # ------------------------------------------------------------------ #

    def run(self, *, max_cycles: int = 8) -> CompositeResult:
        started = time.monotonic()
        result = CompositeResult(plan=self.plan)

        # 分层结果单独发一条结构化事件：界面要画的是那张分层图，不是日志文本
        self._event("plan", payload=self.plan.to_dict())
        for issue in self.plan.issues:
            self._say(f"[PLAN] {issue.kind}: {issue.detail} {list(issue.tasks)}")

        # 拆解复核放在**派发之前**：拆错了就不该开跑，跑完再发现就白烧了。
        if self.root_goal:
            self.review = self.architect.review_decomposition(self.root_goal, self.specs)
            result.review = self.review
            self._event("review", payload=self.review.to_dict())
            for i in self.review.structural:
                self._say(f"[REVIEW] 结构 {i.kind}: {i.detail} {list(i.tasks)}")
            who = "独立复核者" if self.review.independent else "拆解者自己"
            self._say(f"[REVIEW] 复核者 {self.review.reviewer}（{who}）")
            if self.review.sufficient:
                self._say("[REVIEW] 验收标准反推：覆盖完整")
            else:
                for m in self.review.missing:
                    self._say(f"[REVIEW] 可能遗漏: {m}")

        for i, layer in enumerate(self.plan.layers, start=1):
            self._say(
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
        # 注册活的 Orchestrator：服务层的人介入（HUMAN_INTERVENTION）要拿到
        # 它的 bus 才能抢占 —— 注册表归调度层持有，跑完就摘
        if self.registry is not None:
            self.registry[spec.id] = orch
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
        try:
            return spec.id, orch.run(max_cycles=max_cycles)
        finally:
            if self.registry is not None:
                self.registry.pop(spec.id, None)

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
            self._say(f"[CONFLICT] {resource} <- {sorted(tids)}（归属 {owner}）")
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
