"""Runner：plan 注册表 + 活任务注册表 + 线程编排。

一次 run 是阻塞的（§10.1 地基不动），所以全部执行都在 daemon 线程里；
Store 是线程安全的，但**同一个任务不会并发跑两次**（ruling 前检查注册表）。

后端实例在**每次起跑时现建**：设置页改的 key / 模型 / 挡位对新的一次立即生效，
不用重启服务。
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import ids, views
from ..agent.architect import (
    Architect,
    DecompositionResult,
    HumanRuling,
    SpecTemplate,
)
from ..orchestrator import Orchestrator
from ..policy import DEFAULT_POLICY
from ..scheduler import Scheduler
from ..types import Action, SandboxProfile, TaskSpec, TaskStatus

# PROVIDERS 预设表和后端工厂目前住在 cli 里 —— 服务层复用同一份，
# 不另抄（将来值得挪进 llm/，那是另一次整理）
from ..cli import _make_backend, _make_routing_backend, available_providers, resolve_reviewer
from ..llm.routing import assign_providers
from .gate import ChatGate
from .tap import EventHub, TapStore


@dataclass
class PlanEntry:
    """一次拆解的注册项。specs 会被人裁决 / 模型分配改写，result 保持原样。"""

    id: str
    goal: str
    created_at: float
    result: DecompositionResult | None = None
    error: str | None = None
    specs: list[TaskSpec] = field(default_factory=list)
    dispatchable: bool = False
    ruling_note: str = ""
    profiles: list | None = None
    dispatched_root: str | None = None


class Runner:
    def __init__(
        self,
        store: TapStore,
        hub: EventHub,
        *,
        default_backend: str = "deepseek",
        workspace: str | None = None,
        max_cycles: int = 8,
        backend_factory=None,  # 测试注入用：() -> Backend
    ) -> None:
        self.store = store
        self.hub = hub
        self.default_backend = default_backend
        self.workspace = workspace
        self.max_cycles = max_cycles
        self._backend_factory = backend_factory
        self.gate = ChatGate(hub)
        self.running: dict[str, Orchestrator] = {}
        self.plans: dict[str, PlanEntry] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 后端
    # ------------------------------------------------------------------ #

    def _exec_backend(self):
        if self._backend_factory is not None:
            return self._backend_factory()
        providers = available_providers()
        if not providers:
            raise RuntimeError(
                "没有任何供应商的 API key —— 在设置页或 .env 里配一个"
            )
        default = (
            self.default_backend
            if self.default_backend in providers
            else next(iter(providers))
        )
        if len(providers) < 2:
            return _make_backend(default)
        return _make_routing_backend(default, providers)

    def _reviewer_backend(self):
        # 注入了 backend_factory（测试/定制部署）时不自作主张配复核者 ——
        # 否则测试环境会对着真实供应商发请求
        if self._backend_factory is not None:
            return None
        name = resolve_reviewer(self.default_backend, "auto")
        if not name or name not in available_providers():
            return None
        return _make_backend(name)

    def _log(self, msg: str) -> None:
        self.hub.publish_threadsafe({"type": "server-log", "text": msg})

    # ------------------------------------------------------------------ #
    # 拆解（POST /tasks）
    # ------------------------------------------------------------------ #

    def start_plan(self, goal: str) -> str:
        backend = self._exec_backend()  # 同步建一次：没 key 时让请求立刻 400，而不是线程里悄悄死
        plan_id = ids.task_id()  # 同时也是将来那条复合线程的 root_id
        with self._lock:
            self.plans[plan_id] = PlanEntry(id=plan_id, goal=goal, created_at=time.time())
        threading.Thread(
            target=self._plan_worker, args=(plan_id, goal, backend), daemon=True
        ).start()
        return plan_id

    def _plan_worker(self, plan_id: str, goal: str, backend) -> None:
        entry = self.plans[plan_id]
        try:
            ws = (
                Path(self.workspace) / plan_id
                if self.workspace
                else Path(tempfile.mkdtemp(prefix="cowork-serve-"))
            )
            ws.mkdir(parents=True, exist_ok=True)
            architect = Architect(
                backend,
                self.store,
                policy=DEFAULT_POLICY,
                human_gate=self.gate,
                reviewer_backend=self._reviewer_backend(),
            )
            result = architect.plan(
                goal,
                SpecTemplate(
                    sandbox=SandboxProfile(
                        workspace=str(ws), allowed_binaries=("python",)
                    ),
                    parent_id=plan_id,
                ),
                log=self._log,
            )
            with self._lock:
                entry.result = result
                entry.specs = list(result.specs)
                entry.dispatchable = result.accepted
        except Exception as exc:  # noqa: BLE001 - 线程里任何异常都得落成状态
            entry.error = f"{type(exc).__name__}: {exc}"
        self.hub.publish_threadsafe({"type": "plan", "plan_id": plan_id})

    def get_plan(self, plan_id: str) -> dict | None:
        entry = self.plans.get(plan_id)
        if entry is None:
            return None
        if entry.result is None:
            return {
                "plan_id": plan_id,
                "goal": entry.goal,
                "status": "ERROR" if entry.error else "RUNNING",
                "error": entry.error,
            }
        out = entry.result.to_dict()
        out.update(
            plan_id=plan_id,
            dispatchable=entry.dispatchable,
            ruling_note=entry.ruling_note,
            dispatched_root=entry.dispatched_root,
        )
        # 模型选择界面要的两样素材：任务特点（架构师一次调用）+ 可用的家。
        # profiles 是 LLM 调用，惰性生成一次然后缓存 —— 只有一家可用时不该问也不该花。
        providers = available_providers()
        out["available_providers"] = providers
        if entry.dispatchable and len(providers) >= 2:
            if entry.profiles is None:
                try:
                    profiles, _tokens = self._exec_backend().profile_tasks(entry.specs)
                    entry.profiles = [
                        p.to_dict() if hasattr(p, "to_dict") else p.__dict__
                        for p in profiles
                    ]
                except Exception:  # noqa: BLE001 - profiles 是锦上添花，拿不到就不给
                    entry.profiles = None
            if entry.profiles is not None:
                out["profiles"] = entry.profiles
        return out

    def rule_plan(
        self,
        plan_id: str,
        *,
        accept: bool,
        rationale: str = "",
        specs: list[dict] | None = None,
    ) -> None:
        entry = self.plans.get(plan_id)
        if entry is None:
            raise KeyError(plan_id)
        if entry.dispatched_root:
            raise ValueError("已经派发了")
        if specs:
            entry.specs = [TaskSpec.from_dict(s) for s in specs]
            entry.dispatchable = True
            entry.ruling_note = rationale or "人直接交了一份自己的拆解"
        elif accept:
            entry.dispatchable = True
            entry.ruling_note = rationale or "人确认拆解"
        else:
            entry.dispatchable = False
            entry.ruling_note = rationale or "人否决了这份拆解"
        self.hub.publish_threadsafe({"type": "plan", "plan_id": plan_id})

    def dispatch(self, plan_id: str, assignments: dict[str, str] | None = None) -> str:
        entry = self.plans.get(plan_id)
        if entry is None:
            raise KeyError(plan_id)
        if entry.dispatched_root:
            raise ValueError("已经派发了")
        if not entry.dispatchable or not entry.specs:
            raise ValueError("拆解还没通过（或还没答复），不能派发")
        specs = assign_providers(entry.specs, assignments or {}, available_providers())
        sched = Scheduler(
            specs,
            backend=self._exec_backend(),
            store=self.store,
            human_gate=self.gate,
            log=self._log,
            registry=self.running,
        )
        entry.dispatched_root = sched.root_id
        threading.Thread(
            target=self._sched_worker, args=(sched,), daemon=True
        ).start()
        self.hub.publish_threadsafe({"type": "plan", "plan_id": plan_id})
        return sched.root_id or plan_id

    def _sched_worker(self, sched: Scheduler) -> None:
        try:
            sched.run(max_cycles=self.max_cycles)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[SERVE] 调度异常: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # 执行中的任务
    # ------------------------------------------------------------------ #

    def intervene(self, task_id: str, instruction: str) -> bool:
        orch = self.running.get(task_id)
        if orch is None:
            return False
        orch.intervene(instruction)
        return True

    # ------------------------------------------------------------------ #
    # restore 路径（M6 §9 最实质的那块）
    # ------------------------------------------------------------------ #

    def rule_task(
        self,
        task_id: str,
        *,
        action: str,
        rationale: str,
        spec_changes: dict | None = None,
    ) -> None:
        if task_id in self.running:
            raise ValueError("任务正在运行 —— 要改变它请用 intervene，不是 ruling")
        state = self.store.load_task(task_id)
        if state is None:
            raise KeyError(task_id)
        if state.status is not TaskStatus.AWAITING_HUMAN:
            raise ValueError(
                f"只有 AWAITING_HUMAN 的任务能裁决（当前 {state.status.value}）"
            )
        ruling = HumanRuling(
            action=Action(action),
            rationale=rationale,
            spec_changes=spec_changes or {},
        )
        backend = self._exec_backend()
        threading.Thread(
            target=self._restore_worker,
            args=(task_id, ruling, backend),
            daemon=True,
        ).start()

    def _restore_worker(self, task_id: str, ruling: HumanRuling, backend) -> None:
        try:
            orch = Orchestrator.restore(
                task_id,
                backend=backend,
                store=self.store,
                policy=DEFAULT_POLICY,
                human_gate=self.gate,
                log=self._log,
            )
            self.running[task_id] = orch
            orch.resume_with_ruling(ruling, self.max_cycles)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[SERVE] restore {task_id} 失败: {type(exc).__name__}: {exc}")
        finally:
            self.running.pop(task_id, None)

    # ------------------------------------------------------------------ #
    # 读侧（直接透传 views）
    # ------------------------------------------------------------------ #

    def list_threads(self):
        return views.thread_list(self.store)

    def get_detail(self, task_id: str, after_seq: int = 0):
        return views.task_detail(self.store, task_id, after_seq=after_seq)
