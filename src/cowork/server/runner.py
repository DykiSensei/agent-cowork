"""Runner：plan 注册表 + 活任务注册表 + 线程编排。

一次 run 是阻塞的（§10.1 地基不动），所以全部执行都在 daemon 线程里；
Store 是线程安全的，但**同一个任务不会并发跑两次**（ruling 前检查注册表）。

后端实例在**每次起跑时现建**：设置页改的 key / 模型 / 挡位对新的一次立即生效，
不用重启服务。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import ids, views, workspace
from ..agent.architect import (
    Architect,
    DecompositionResult,
    HumanRuling,
    SpecTemplate,
)
from ..orchestrator import Orchestrator
from ..policy import DEFAULT_POLICY
from ..scheduler import Scheduler
from ..types import Action, SandboxProfile, TaskEvent, TaskSpec, TaskStatus  # noqa: F401

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
    # 产物落在哪、是不是接手已有项目。**要能回给界面** ——
    # 「我的产物在哪」原来在这套系统里没有答案（落在随机临时目录，界面也不显示）
    workspace: str = ""
    takeover: bool = False


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

    def _role_provider(self, key: str, providers: dict[str, str]) -> str | None:
        """设置页给某个角色指定的供应商。不认识 / 没配 key 的一律当没设。

        **每次起跑时读**，和后端实例一样 —— 设置页改完对下一个任务立即生效。
        """
        import os

        name = (os.environ.get(key) or "").strip()
        if not name:
            return None
        if name == "none":
            return "none"
        return name if name in providers else None

    def _exec_backend(self):
        if self._backend_factory is not None:
            return self._backend_factory()
        providers = available_providers()
        if not providers:
            raise RuntimeError(
                "没有任何供应商的 API key —— 在设置页或 .env 里配一个"
            )
        # 架构师那一家由设置页决定（§10.3.3 的「模型选择归人」在角色这一层的落地）：
        # 它是唯一写入决策点，拆解 / 中断决策 / 验收 / 分诊全走它。
        default = (
            self._role_provider("COWORK_ARCHITECT_PROVIDER", providers)
            or (self.default_backend if self.default_backend in providers else None)
            or next(iter(providers))
        )
        if len(providers) < 2:
            return _make_backend(default)
        # Subagent 可以指定另一家：它干活、架构师做判断，这两件事适合的模型不一定同一个。
        # 没指定就跟架构师同一家（RoutingBackend 的 default）。
        return _make_routing_backend(
            default,
            providers,
            subagent=self._role_provider("COWORK_SUBAGENT_PROVIDER", providers),
        )

    def _reviewer_backend(self):
        # 注入了 backend_factory（测试/定制部署）时不自作主张配复核者 ——
        # 否则测试环境会对着真实供应商发请求
        if self._backend_factory is not None:
            return None
        providers = available_providers()
        picked = self._role_provider("COWORK_REVIEWER_PROVIDER", providers)
        if picked == "none":
            # 人明确关掉了独立复核 —— 退回同模型复核（M5b 的形态），不是不复核
            return None
        name = picked or resolve_reviewer(self.default_backend, "auto")
        if not name or name not in providers:
            return None
        return _make_backend(name)

    def _review_writes(self) -> bool:
        """写入侧复核开关（设置页 → .env → 这里）。

        **每次起跑时读**，和后端实例一样 —— 设置页改完对下一个任务立即生效，
        不用重启服务。默认开（§11.19）。
        """
        import os

        return (os.environ.get("COWORK_REVIEW_WRITES") or "on").strip().lower() != "off"

    def _allowed_binaries(self) -> tuple[str, ...]:
        """`run` 能调哪些可执行文件。默认是各语言的运行时（`types.DEFAULT_BINARIES`）。

        **白名单归人和模板，不归架构师**（同 `SpecTemplate` 那条：让被隔离方
        给自己配隔离边界是没有意义的）。所以它在设置页，不在拆解提示词里。
        """
        import os

        from ..types import DEFAULT_BINARIES

        raw = (os.environ.get("COWORK_ALLOWED_BINARIES") or "").strip()
        if not raw:
            return DEFAULT_BINARIES
        return tuple(x.strip() for x in raw.split(",") if x.strip()) or DEFAULT_BINARIES

    def _allow_network(self) -> bool:
        """两个联网工具开不开。**默认关** —— 见 `SpecTemplate.tools` 的说明。"""
        import os

        return (os.environ.get("COWORK_ALLOW_NETWORK") or "").strip().lower() == "on"

    def _network_tools(self) -> tuple[str, ...]:
        """联网开着时，实际放进白名单的那几个。

        **`search_web` 还要看有没有配搜索 key**：白名单里放一个调了必然失败的
        工具，模型会去调、会白费一步 —— 那正是 §11.6f「工具面的缺口表现成
        白烧一轮」的反面版本。没 key 就当没有这个工具，`fetch_url` 照常给。
        """
        if not self._allow_network():
            return ()
        from ..runtime import search as search_api

        return ("fetch_url", "search_web") if search_api.configured() else ("fetch_url",)

    def _log(self, msg: str) -> None:
        self.hub.publish_threadsafe({"type": "server-log", "text": msg})

    def _plan_log(self, plan_id: str):
        """拆解过程的日志 —— **落成事件**，不只是广播。

        原来 `architect.plan()` 的日志走 `self._log`，那只是一条 SSE 广播：
        没人订阅时它就消失了，刷新页面也拿不回来。于是「架构师在干什么」在界面上
        是一段空白 —— 实测反馈的原话是「不知道它到底在干啥，卡住了也不知道」。

        写在 root 线程上（那条线程此时只有事件、还没有任何 tasks 行 ——
        这正是 `views` 那条「线程的存在性看事件」要支撑的场景）。
        """
        def log(msg: str) -> None:
            self._log(msg)
            try:
                self.store.append_event(
                    TaskEvent(task_id=plan_id, kind="log", text=msg)
                )
            except Exception:  # noqa: BLE001 - 事件是旁路，写不进去不该影响拆解
                pass

        return log

    # ------------------------------------------------------------------ #
    # 拆解（POST /tasks）
    # ------------------------------------------------------------------ #

    def workspace_root(self) -> Path:
        """默认工作区：设置页 > 启动参数 > `~/cowork-workspaces`。

        **必须是人找得到的地方**：原来没配就 `tempfile.mkdtemp()`，
        于是「我的产物在哪」这个问题没有答案（实测反馈）。
        """
        import os

        raw = (os.environ.get("COWORK_WORKSPACE") or "").strip() or self.workspace
        return workspace.resolve_workspace(raw) if raw else workspace.default_root()

    def start_plan(
        self, goal: str, *, ws: str | None = None, takeover: bool = False
    ) -> str:
        backend = self._exec_backend()  # 同步建一次：没 key 时让请求立刻 400，而不是线程里悄悄死
        plan_id = ids.task_id()  # 同时也是将来那条复合线程的 root_id
        # 路径问题要在起跑之前抛（ValueError → 400），别等到 Subagent 写第一个
        # 文件才发现目录不能用
        root = workspace.resolve_workspace(ws) if ws else self.workspace_root()
        ws_path = workspace.task_workspace(root, plan_id, takeover=takeover)
        with self._lock:
            self.plans[plan_id] = PlanEntry(
                id=plan_id, goal=goal, created_at=time.time(),
                workspace=str(ws_path), takeover=takeover,
            )
        # 人的原话落进 root 线程的第一条事件（M6 §9 那两条小缺口）。
        # 为什么非记不可：spec.goal 会被架构师改写，rev>1 之后**人最初说的那句话
        # 就再也拿不回来了**，而界面开头那个「你发布任务」的气泡要的正是原话。
        # 同时这也是复合线程 root_goal 的落库处 —— 两条缺口是同一件事。
        # 写在 root_id 上，而 root 没有 tasks 行：events 上不能有外键，见 schema.sql。
        self.store.append_event(
            TaskEvent(task_id=plan_id, kind="human", text=goal)
        )
        threading.Thread(
            target=self._plan_worker, args=(plan_id, goal, backend), daemon=True
        ).start()
        return plan_id

    def _plan_worker(self, plan_id: str, goal: str, backend) -> None:
        entry = self.plans[plan_id]
        log = self._plan_log(plan_id)
        try:
            log(f"[PLAN] 开始拆解：{goal}")
            ws = workspace.ensure(Path(entry.workspace))
            log(f"[PLAN] 产物会落在 {ws}")
            # 接手已有项目：先把现状摆给架构师看。**不给的话它会把一个有内容的
            # 目录当空目录，从零重建一遍** —— 那正是「半路接手」和「从零开始」
            # 的全部区别。从零开始的任务不采集（目录本来就是空的，白花提示词）。
            existing = ""
            if entry.takeover:
                entries = workspace.snapshot(ws)
                existing = workspace.render_snapshot(entries)
                log(
                    f"[PLAN] 接手已有项目，看到 {len(entries)} 个文件"
                    if entries
                    else f"[PLAN] 接手模式，但 {ws} 是空的 —— 按从零开始处理"
                )
            architect = Architect(
                backend,
                self.store,
                policy=DEFAULT_POLICY,
                human_gate=self.gate,
                reviewer_backend=self._reviewer_backend(),
                review_writes=self._review_writes(),
            )
            template = SpecTemplate(
                sandbox=SandboxProfile(
                    workspace=str(ws), allowed_binaries=self._allowed_binaries()
                ),
                parent_id=plan_id,
            )
            net = self._network_tools()
            if net:
                template = replace(template, tools=(*template.tools, *net))
                log(f"[PLAN] 联网已开：{', '.join(net)}")
                if "search_web" not in net:
                    # **说出来**：否则「开了联网却搜不了」在界面上是一段沉默，
                    # 而人手上唯一的线索是子任务绕远路的执行记录。
                    log("[PLAN] 没配搜索 key（ZHIPUAI_API_KEY），search_web 这次不给")
            reviewer = self._reviewer_backend()
            log(
                f"[PLAN] 生成者 {getattr(backend, 'name', '?')}"
                f" / 复核者 {getattr(reviewer, 'name', '（同生成者）') if reviewer else '（同生成者）'}"
            )
            result = architect.plan(
                goal, template, existing=existing or None, log=log
            )
            log(
                f"[PLAN] 终局 {result.status}：{len(result.specs)} 个子任务，"
                f"{result.attempts} 轮，{result.tokens} token"
            )
            with self._lock:
                entry.result = result
                entry.specs = list(result.specs)
                entry.dispatchable = result.accepted
        except Exception as exc:  # noqa: BLE001 - 线程里任何异常都得落成状态
            entry.error = f"{type(exc).__name__}: {exc}"
            log(f"[PLAN] 拆解失败：{entry.error}")
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
            # 产物落在哪 —— 界面要显示它，这是「我的东西在哪」的唯一答案
            workspace=entry.workspace,
            takeover=entry.takeover,
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
            reviewer_backend=self._reviewer_backend(),
            review_writes=self._review_writes(),
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

    def cancel(self, task_id: str, reason: str = "") -> bool:
        """停掉正在跑的任务。返回 False = 它不在跑（已经终局，或还没派发）。

        和 `rule_task(action=ABANDON)` 是两件事：那条管 AWAITING_HUMAN 的任务
        （走 restore 重建现场），这条管**正在烧钱的**那些。两者合起来才覆盖全部
        「我要它停」的场景。
        """
        orch = self.running.get(task_id)
        if orch is None:
            return False
        orch.cancel(reason)
        return True

    def delete_thread(self, task_id: str) -> tuple[bool, str]:
        """删掉一条线程。返回 (成不成, 原因)。

        **正在跑的不能删** —— 那不是删除，是「一边跑一边把它的记录抽走」，
        后面每一次 save_task 都会把行写回来，删了等于没删。先取消，再删。
        """
        running = [tid for tid in self.running if tid == task_id]
        running += [
            tid
            for tid, orch in list(self.running.items())
            if orch.spec.parent_id == task_id
        ]
        if running:
            return False, (
                "这条任务还在跑，先点「停下来」，等它停了再删 —— "
                "边跑边删的话记录会被它自己写回来"
            )
        deleter = getattr(self.store, "delete_thread", None)
        if deleter is None:
            return False, "这个存储实现不支持删除"
        deleter(task_id)
        # 内存里的拆解注册表也要清 —— 不清的话列表里那一行会被
        # 「有事件的线程」那条规则重新变出来（plans 里还留着它的 goal）
        with self._lock:
            self.plans.pop(task_id, None)
        self.hub.publish_threadsafe({"type": "task", "task_id": task_id,
                                     "status": "DELETED"})
        return True, ""

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
                reviewer_backend=self._reviewer_backend(),
                review_writes=self._review_writes(),
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
