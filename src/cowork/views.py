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

    rows.sort(key=lambda r: (r["updated_at"] or 0, r["task_id"]), reverse=True)
    return rows


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
    if children:
        return _composite_detail(store, task_id, children, after_seq)

    state = store.load_task(task_id)
    if state is None:
        return None
    signals = {s.id: s.to_dict() for s in store.signals_for(task_id)}
    decisions = {d.id: d.to_dict() for d in store.decisions_for(task_id)}
    events = [e.to_dict() for e in _events(store, task_id, after_seq)]
    return {
        "kind": "single",
        "state": state.to_dict(),
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
    }


def _events(store, task_id: str, after_seq: int) -> list:
    getter = getattr(store, "events_for", None)
    if getter is None:
        return []
    return getter(task_id, after_seq)
