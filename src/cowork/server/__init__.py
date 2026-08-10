"""M6 服务层：把 `cowork.views` 的投影 + 人的介入/裁决暴露成 HTTP + SSE。

设计约束（M6-界面层接口.md §0 / §7）：

- **单进程**：runner 跑在本进程的线程里，人的介入是一次函数调用，状态同步
  不需要跨进程机制（LISTEN/NOTIFY 留到分进程那天再说）。
- **不碰控制流**：run() 仍然是阻塞的，并发靠线程；抢占的全部实现仍然是
  step 循环开头的一次 take_preempt()。
- **架构师是唯一写入决策点**：裁决经 `Architect.apply_human_ruling()` 落地，
  服务层没有任何直接改 spec 的路径。
"""

from .app import create_app

__all__ = ["create_app"]
