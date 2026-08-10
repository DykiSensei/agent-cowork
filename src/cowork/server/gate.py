"""ChatGate：群聊场景下的 HumanGate 实现（M6 §4.2）。

三条通道的共同策略是**把问题摆出来、立即返回** —— 人不会秒回，任务落在
AWAITING_HUMAN 等着，答复从 HTTP 端点进来：

- `review()`：升级问题已经由占位 DecisionRecord + `views.pending_ruling()`
  落库，这里只广播一声「有活要人干」。人答复发到 `POST /tasks/{id}/ruling`，
  服务层走 restore 路径（`Orchestrator.restore` + `resume_with_ruling`）。
- `review_plan()`：plan 停在 AWAITING_HUMAN，由 plan 注册表持有，
  答复走 `POST /plans/{id}/ruling`。
- `assign_models()`：**服务化之后「问人选模型」从网关同步问答变成 HTTP 往返**，
  所以这里恒返回空分配（全用默认那家）；人的选择经
  `POST /plans/{id}/dispatch` 的 assignments 进来。
"""

from __future__ import annotations

from .tap import EventHub


class ChatGate:
    def __init__(self, hub: EventHub) -> None:
        self.hub = hub

    def review(self, spec, signals, verdict, reason) -> None:
        self.hub.publish_threadsafe(
            {"type": "awaiting", "task_id": spec.id, "reason": reason}
        )
        return None

    def review_plan(self, root_goal, specs, review, reason) -> None:
        return None

    def assign_models(self, profiles, providers) -> dict[str, str]:
        return {}
