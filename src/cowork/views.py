"""界面层要的投影：Store 里的对象 → M6 契约里的形状（`M6-界面层接口.md`）。

**这一层没有业务逻辑，只有取数和拼装。** 放在这里而不是让服务层自己拼，理由有两条：

1. 形状是**对外承诺**。界面层照着 `M6-界面层接口.md` 写死了字段名，那份承诺应该有
   一处代码实现、一处测试盯着，而不是散在未来某个 FastAPI 的路由函数里。
2. 服务层还不存在。等它存在时，路由函数应该只剩「调用这里 + 序列化」。

不做的事：不查库以外的东西、不发起模型调用、不改任何状态。
"""

from __future__ import annotations

from typing import Any

from .types import TaskState, TaskStatus

# 终局状态：列表里用来判断「这条线程还会不会动」
TERMINAL = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABANDONED}
)


def thread_summary(state: TaskState, *, composite: bool = False) -> dict[str, Any]:
    """列表项（`GET /tasks`）。

    **只放列表要渲染的字段**，不带 spec —— 侧边栏有几十条线程时，每条都驮一份
    完整 spec 是几百 KB 的无用流量。要详情就点进去拿 `task_detail()`。

    `title` 取 goal 的第一行：goal 可能很长甚至带换行，列表要的是一句话。
    """
    goal = state.spec.goal.strip()
    title = goal.splitlines()[0] if goal else state.spec.id
    return {
        "task_id": state.spec.id,
        "title": title[:80],
        "status": state.status.value,
        "composite": composite,
        "tokens_used": state.tokens_used,
        "revision": state.spec.revision,
        "current_step": state.current_step,
        "terminal": state.status in TERMINAL,
        "updated_at": state.started_at,
    }


def thread_list(store) -> list[dict[str, Any]]:
    """`GET /tasks`：所有线程，子任务折进父任务。

    复合任务在存储里是「一个父 id + 若干带 parent_id 的子任务」，而界面左侧栏
    要的是**一条线程**。所以这里按 parent_id 归并：有子任务的父任务标
    `composite=true`，子任务不单独占一行（它们在详情里出现）。

    父任务本身可能不在 tasks 表里（`Scheduler` 拿到的是一组现成的 spec，
    没有人建过那个父任务），那就用 parent_id 合成一条 —— 否则复合任务在
    列表里会整个消失。

    **「还没有任何子任务」的线程也要收**：`POST /tasks` 一落地就写了人的原话
    （一条 `human` 事件），但子任务要等派发之后各自的 Orchestrator 起跑才有
    tasks 行。中间那一段（拆解中、刚派发）线程只存在于 events 里 —— 不收的话
    用户刚发布的任务在侧栏里根本不出现，而它明明正在花钱。
    """
    states = list(store.list_tasks())
    by_id = {s.spec.id: s for s in states}
    children: dict[str, list[TaskState]] = {}
    for s in states:
        if s.spec.parent_id:
            children.setdefault(s.spec.parent_id, []).append(s)

    rows: list[dict[str, Any]] = []
    for s in states:
        if s.spec.parent_id:
            continue  # 子任务不单独占一行
        rows.append(thread_summary(s, composite=bool(children.get(s.spec.id))))

    for parent_id, kids in children.items():
        if parent_id in by_id:
            continue  # 父任务是真实存在的任务，上面已经收了
        rows.append(_synthetic_parent(parent_id, kids, _root_goal(store, parent_id)))

    seen = {r["task_id"] for r in rows}
    for tid in _event_only_threads(store):
        if tid in seen or tid in by_id:
            continue
        rows.append(_pending_thread(store, tid))

    rows.sort(key=lambda r: (r["updated_at"] or 0, r["task_id"]), reverse=True)
    return rows


def _event_only_threads(store) -> list[str]:
    """有事件的线程 id。老 Store 没有这个方法就当没有（返回空）。"""
    getter = getattr(store, "event_task_ids", None)
    return list(getter()) if getter else []


def _pending_thread(store, task_id: str) -> dict[str, Any]:
    """只有事件、还没有任何任务行的线程 —— 「已发布，还没跑起来」。

    状态给 PENDING 而不是 RUNNING：这时候确实还没有任何 Subagent 在跑。
    拆解本身是架构师在花钱，那件事的进度在详情页的时间线里看得到。
    """
    events = _events(store, task_id, 0)
    goal = _root_goal(store, task_id)
    title = goal.splitlines()[0][:80] if goal else "新任务（拆解中）"
    return {
        "task_id": task_id,
        "title": title,
        "status": TaskStatus.PENDING.value,
        "composite": True,
        "tokens_used": 0,
        "revision": 1,
        "current_step": 0,
        "terminal": False,
        "updated_at": events[0].created_at if events else None,
    }


def _root_goal(store, task_id: str) -> str:
    """复合线程开头那句人话：root 线程的第一条 `human` 事件。

    服务层在 `POST /tasks` 时把人的原话写在这里（M6 §9）。拿不到就返回空串 ——
    `cli composite` 那种没有服务层的入口不会有这条事件，那时候退回「复合任务（N）」
    这个合成标题，**不要拿子任务的 goal 顶替**：那是架构师写的，不是人说的。
    """
    for e in _events(store, task_id, 0):
        if e.kind == "human":
            return (e.text or "").strip()
    return ""


def _synthetic_parent(
    parent_id: str, kids: list[TaskState], root_goal: str = ""
) -> dict[str, Any]:
    """给「只有子任务、没有父任务记录」的复合任务合成一条列表项。

    状态按最坏情况取：有人等人 → 等人；有人失败 → 失败；全完成才叫完成。
    **不取「多数」也不取「第一个」** —— 复合任务里一个子任务挂起，整件事就是挂起的。
    """
    statuses = {k.status for k in kids}
    if TaskStatus.AWAITING_HUMAN in statuses:
        status = TaskStatus.AWAITING_HUMAN
    elif statuses & {TaskStatus.FAILED, TaskStatus.ABANDONED}:
        status = TaskStatus.FAILED
    elif statuses == {TaskStatus.COMPLETED}:
        status = TaskStatus.COMPLETED
    elif TaskStatus.RUNNING in statuses or TaskStatus.INTERRUPTED in statuses:
        status = TaskStatus.RUNNING
    else:
        status = TaskStatus.PENDING
    started = [k.started_at for k in kids if k.started_at]
    title = root_goal.splitlines()[0][:80] if root_goal else f"复合任务（{len(kids)} 个子任务）"
    return {
        "task_id": parent_id,
        "title": title,
        "status": status.value,
        "composite": True,
        "tokens_used": sum(k.tokens_used for k in kids),
        "revision": max(k.spec.revision for k in kids),
        "current_step": sum(k.current_step for k in kids),
        "terminal": status in TERMINAL,
        "updated_at": max(started) if started else None,
    }


def task_detail(store, task_id: str, *, after_seq: int = 0) -> dict[str, Any] | None:
    """`GET /tasks/{id}`：状态快照 + 时间线 + 正文。

    单任务和复合任务共用一层外壳，靠 `kind` 区分 —— 界面左侧点进来的时候
    并不知道点开的是哪种。

    时间线的顺序**只由 `events.seq` 决定**，这里不重排 —— 并行任务的
    `created_at` 会撞在同一毫秒上，按时间排会让前端的追加式渲染错位。

    `after_seq` 传上次拿到的最大 seq 就是增量拉取，够 SSE 断线重连用。
    """
    children = [s for s in store.list_tasks() if s.spec.parent_id == task_id]
    state = store.load_task(task_id)
    # 「线程存在但还没有任何任务行」也要给得出详情：`POST /tasks` 之后到
    # 子任务起跑之间有一段真空期，而界面在派发成功那一刻就会切过来。
    # 那时候回 404 的话，前端拿到的是一个错误 —— 实测就是这样：刚发布完
    # 整页变成「连不上服务」，刷新一下又好了（那时子任务已经起来了）。
    # **线程的存在性看事件，不看 tasks 行**（同 §11.18 那条：事件是线程级的）。
    if children or (state is None and _events(store, task_id, 0)):
        return _composite_detail(store, task_id, children, after_seq)

    if state is None:
        return None
    signals = {s.id: s.to_dict() for s in store.signals_for(task_id)}
    decisions = {d.id: d.to_dict() for d in store.decisions_for(task_id)}
    events = [e.to_dict() for e in _events(store, task_id, after_seq)]
    return {
        "kind": "single",
        "state": state.to_dict(),
        # 单任务也要有「此刻在做什么」—— 复合任务那边是一份 map，这边是一份
        "progress": task_progress(store, state),
        "events": events,
        # 正文按 id 索引给出：事件里只有 ref_id，界面拿它查这两张表。
        # 内联进事件会让同一条信号在响应里出现两次，而它们会很长。
        "signals": signals,
        "decisions": decisions,
        "pending": pending_ruling(store, task_id),
    }


def _composite_detail(
    store, task_id: str, children: list[TaskState], after_seq: int
) -> dict[str, Any]:
    """复合任务的详情：分层图和复核结论从事件里取，不重算。

    `plan` / `review` 是 `Scheduler` 跑的时候算出来并落成事件的。这里**不重新调
    `build_plan()`**：那会把「当时是怎么分层的」换成「现在按最新 spec 会怎么分」，
    而中途改过 scope 的话两者不一样 —— 时间线要的是当时那份。
    """
    events = [e.to_dict() for e in _events(store, task_id, after_seq)]
    plan = _last_payload(store, task_id, "plan")
    review = _last_payload(store, task_id, "review")
    signals: dict[str, Any] = {}
    decisions: dict[str, Any] = {}
    for kid in children:
        signals.update({s.id: s.to_dict() for s in store.signals_for(kid.spec.id)})
        decisions.update({d.id: d.to_dict() for d in store.decisions_for(kid.spec.id)})
    return {
        "kind": "composite",
        "state": None,
        # **每个在等人的子任务，等的是什么**。原来只给 `pending_children`
        # 一串 id：界面知道「有人在等」，却拿不到升级原因和系统建议，
        # 于是复合线程上根本渲染不出裁决表单 —— 而子任务被折进父线程、
        # 侧栏里点不到，人因此完全无处答复（实测卡在这里）。
        "pending": {
            k.spec.id: pending_ruling(store, k.spec.id)
            for k in children
            if k.status is TaskStatus.AWAITING_HUMAN
        },
        # 「此刻各自在做什么」：进度、成本、最后一个动作。
        # 没有它的时候界面上只有一个状态点，人看不出系统是在干活还是卡住了。
        "progress": {k.spec.id: task_progress(store, k) for k in children},
        # 人最初说的那句话。它也在 events 里（第一条 human 事件），这里再给一份是
        # 因为界面的标题栏要它，而标题栏不该去翻时间线
        "root_goal": _root_goal(store, task_id),
        "plan": plan,
        "review": review,
        "tasks": {k.spec.id: k.to_dict() for k in children},
        "events": events,
        "signals": signals,
        "decisions": decisions,
        # 复合线程本身不会挂起，挂起的是某个子任务 —— 谁在等人要能一眼看出来
        "pending_children": [
            k.spec.id for k in children if k.status is TaskStatus.AWAITING_HUMAN
        ],
    }


def task_progress(store, state: TaskState) -> dict[str, Any]:
    """这个任务此刻在做什么 —— 界面上「进度」那一栏的全部素材。

    **只回答确定性的问题**：跑到第几步、烧了多少、最后一个动作是什么。
    不做任何判断（「是不是卡住了」「顺不顺利」），那要么归架构师、要么归人。

    最后一个动作从最新 checkpoint 的 `reasoning_trace` 末尾取 —— 那是 Subagent
    真正干过的事，比日志行准。终局任务不读 checkpoint：它已经不在做任何事，
    而 checkpoint 里带着整份上下文，读它只是白花 IO。

    动作按**结构**返回、不拼成句子：措辞归界面层（`ui/src/copy.ts` 那一套），
    同一份数据在专业版和简洁版要说成两种话。
    """
    spec = state.spec
    out: dict[str, Any] = {
        "task_id": spec.id,
        "goal": spec.goal,
        "status": state.status.value,
        "terminal": state.status in TERMINAL,
        "revision": spec.revision,
        "agent_id": state.agent_id,
        "current_step": state.current_step,
        "max_steps": spec.max_steps,
        "tokens_used": state.tokens_used,
        "token_budget": spec.token_budget,
        "scope": list(spec.scope),
        # 产物落在哪 —— 「我的东西在哪」得有答案，而它只在 spec.sandbox 里
        "workspace": spec.sandbox.workspace if spec.sandbox else "",
        "last_action": None,
        "last_result": None,
        "produced": [],
    }
    if state.status in TERMINAL or not state.checkpoint_id:
        return out

    loader = getattr(store, "load_checkpoint", None)
    cp = loader(state.checkpoint_id) if loader else None
    if cp is None:
        return out
    ctx = cp.agent_context
    out["produced"] = [a.content_ref for a in ctx.produced]
    for entry in reversed(ctx.reasoning_trace):
        role = entry.get("role")
        if role == "assistant" and out["last_action"] is None:
            action = entry.get("action") or {}
            out["last_action"] = {
                "step": entry.get("step"),
                "kind": action.get("kind"),
                "name": action.get("name"),
                # 工具的第一个参数就是「对什么东西动手」——路径或命令
                "target": _action_target(action),
                "thought": action.get("thought") or "",
            }
        elif role == "tool" and out["last_result"] is None:
            out["last_result"] = {
                "step": entry.get("step"),
                "name": entry.get("name"),
                "ok": entry.get("ok"),
                "exit_code": entry.get("exit_code"),
                # 失败时人最想看的就是这段，成功时不给（stdout 可能很长）
                "stderr": (entry.get("stderr") or "")[-400:] if not entry.get("ok") else "",
            }
        if out["last_action"] and out["last_result"]:
            break
    return out


def _action_target(action: dict[str, Any]) -> str:
    args = action.get("args") or {}
    if "path" in args:
        return str(args["path"])
    if "command" in args:
        return " ".join(str(x) for x in args["command"])
    return action.get("summary") or ""


def _last_payload(store, task_id: str, kind: str) -> dict[str, Any] | None:
    """取某种事件最后一条的 payload。重跑过就以最后一次为准。"""
    hits = [e for e in _events(store, task_id, 0) if e.kind == kind]
    return hits[-1].payload if hits else None


def pending_ruling(store, task_id: str) -> dict[str, Any] | None:
    """这个任务此刻是不是在等人；等的是什么。

    「等人」这件事在存储里是两条线索的交集：`status == AWAITING_HUMAN`，
    以及最后一条带 `escalation_reason` 的裁决。界面的「等你拍板」卡片要的
    `suggestion`（系统建议怎么做）就挂在那条裁决上（M6 §9 第 1 条）。
    """
    state = store.load_task(task_id)
    if state is None or state.status is not TaskStatus.AWAITING_HUMAN:
        return None
    escalated = [d for d in store.decisions_for(task_id) if d.escalation_reason]
    if not escalated:
        # 挂起但没有升级记录：架构师连模型都调不动的那条路径。
        # 这时候没有「系统建议」可展示 —— 如实返回 None，别编一个。
        return {
            "reason": "任务挂起等待人处理",
            "suggestion": None,
            "decision_id": None,
            "checkpoint_id": state.checkpoint_id,
        }
    last = escalated[-1]
    return {
        "reason": last.escalation_reason,
        "suggestion": last.suggestion,
        "decision_id": last.id,
        "checkpoint_id": state.checkpoint_id,
        # 撞了 `run` 的程序白名单时，人要能当场放行（M11）。
        # **从信号的 payload 里取，不从理由文字里抠** —— 后者迟早会因为
        # 改一句话而失效，而失效的方式是按钮悄悄不见了。
        "blocked_binary": _blocked_binary(store, task_id, last),
    }


def _blocked_binary(store, task_id: str, decision) -> str | None:
    """这次挂起是不是「想跑一个没被允许的程序」，是的话是哪个。"""
    triggers = set(decision.trigger or ())
    for sig in store.signals_for(task_id):
        if sig.id in triggers and isinstance(sig.payload, dict):
            binary = sig.payload.get("binary")
            if binary:
                return str(binary)
    return None


def _events(store, task_id: str, after_seq: int) -> list:
    getter = getattr(store, "events_for", None)
    if getter is None:
        return []
    return getter(task_id, after_seq)
