"""写入处发事件（M6 §12 6.4 的单进程答案）。

包一层 Store：写操作先落库、再广播给 SSE 订阅者。状态一致性靠三条保证：

1. **单一写入点** —— 只有 runner 线程写 store；
2. **事件在写入处发出** —— 落库成功才广播，不存在「广播了但没存上」；
3. **可随时回源对账** —— SSE 只是通知，正文永远以 `views.task_detail()` 为准
   （`after_seq` 增量拉取就是给断线重连用的）。

事件丢了没关系：它们是通知不是日志，客户端重拉一次就齐。
"""

from __future__ import annotations

import asyncio


class EventHub:
    """runner 线程 → asyncio 的桥。publish 从任意线程调用都安全。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish_threadsafe(self, msg: dict) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self._publish, msg)

    def _publish(self, msg: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 客户端跟不上就丢通知 —— 它会回源重拉，丢通知不等于丢数据
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


class TapStore:
    """Store 的委托包装：append_event / 状态变化时广播，其余方法原样透传。"""

    def __init__(self, inner, hub: EventHub) -> None:
        self._inner = inner
        self._hub = hub
        self._last_status: dict[str, str] = {}

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def save_task(self, state) -> None:
        self._inner.save_task(state)
        # save_task 每个 step 都会发生，只有状态迁移值得广播
        status = state.status.value
        if self._last_status.get(state.spec.id) != status:
            self._last_status[state.spec.id] = status
            self._hub.publish_threadsafe(
                {"type": "task", "task_id": state.spec.id, "status": status}
            )

    def append_event(self, ev):
        ev = self._inner.append_event(ev)
        self._hub.publish_threadsafe(
            {
                "type": "event",
                "task_id": ev.task_id,
                "seq": ev.seq,
                "kind": ev.kind,
            }
        )
        return ev
