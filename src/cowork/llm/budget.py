"""会话级 token 硬护栏（§12 M9）。

**要解决的缺口**：`TaskSpec.token_budget` 管的是**一个任务**，
`policy.budget_escalation_ratio` 是**软**的（越线只升级、不停）。
于是一次复合运行是「N 个各自有上限的任务」，总量没有任何上限 ——
4 个子任务实测 25.7k（§11.8），但一个反复重做的循环可以把它推到任意大。
真正的硬强制此前只来自 LiteLLM 的 virtual key（§10.3.1），
而那是**可选组件**：直连供应商时那层不存在。
**一个能烧钱的工具不该把唯一的刹车放在可选组件上。**

**做法**：包在 Backend 外面数 token，超了就在**下一次调用之前**抛
`BudgetExceeded`。这个异常本来就是 LiteLLM 硬拒绝时抛的那一个
（`llm/errors.py`，`signal_type = BUDGET_EXCEEDED`），所以它天然汇进既有链路：
Subagent 侧变硬信号交给架构师，架构师侧变「没有决策者」→ AWAITING_HUMAN。
**不需要任何新的控制流** —— 应用层的硬上限和代理层的硬拒绝走同一条路。

为什么包 Backend 而不是在各处加检查：模型调用是唯一花钱的动作，而它们全部
经过 Backend 协议。包一层就是一个拦截点覆盖全部（Subagent 步进、架构师决策、
写入侧复核、拆解、摘要、验收、探查），加新能力时也不会漏掉。

**已知边界：最多会超出一次调用的量。** 检查在调用前、记账在调用后，
而花钱之前无法知道这次要花多少 —— 所以 `limit` 是「停下来的水位线」，
不是「总额不会超过这个数」。按实测单次调用的量级（拆解最贵，16000 上限；
其余多在 1–4k）估，超出量在低万级。要真正的按次预扣，得先能预估每次调用的
成本，那是另一件事 —— 而 LiteLLM 的 virtual key 本来就在做这件事，
这一层要的是「不装那套东西时也有刹车」。
"""

from __future__ import annotations

import threading
from typing import Any

from .errors import BudgetExceeded


class CostGuard:
    """一次会话的累计花费与上限。**多个 Backend 共享同一个实例**。

    复核者、路由到别家的 Subagent、架构师各自是不同的 Backend 对象，
    但它们花的是同一笔钱 —— 每个都包一个自己的护栏等于没有护栏。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        # Scheduler 层内并行跑多个 Orchestrator，它们共享后端也就共享这个计数器
        self._lock = threading.Lock()

    def check(self) -> None:
        """花钱之前问一次。超了抛 BudgetExceeded。"""
        if self.limit <= 0:
            return  # <=0 表示不设限（bench 跑批自己控成本，不需要这层）
        with self._lock:
            if self.used >= self.limit:
                raise BudgetExceeded(
                    f"会话 token 上限已用尽：{self.used}/{self.limit}。"
                    f"用 --budget 调整，或 --budget 0 关闭这道护栏"
                )

    def spend(self, tokens: int) -> None:
        with self._lock:
            self.used += tokens

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used) if self.limit > 0 else -1

    def __repr__(self) -> str:  # 调试与日志用
        cap = self.limit if self.limit > 0 else "∞"
        return f"CostGuard({self.used}/{cap})"


class BudgetedBackend:
    """给任意 Backend 套上会话级上限。

    **只在调用前检查、调用后记账**，不改任何返回值 —— 它对被包的后端是透明的。
    """

    def __init__(self, inner: Any, guard: CostGuard) -> None:
        self.inner = inner
        self.guard = guard

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "?")

    def _call(self, method: str, *args, **kwargs):
        """所有转发走这一条路：先 check 再调，返回值里的 token 数记账。

        约定：Backend 协议的每个方法都把 token 数放在返回元组的**最后一位**。
        这一条是整个模块成立的前提，加新方法时要守住。
        """
        self.guard.check()
        result = getattr(self.inner, method)(*args, **kwargs)
        if isinstance(result, tuple) and result and isinstance(result[-1], int):
            self.guard.spend(result[-1])
        return result

    # -- Backend 协议 ------------------------------------------------------- #

    def next_step(self, ctx):
        return self._call("next_step", ctx)

    def triage(self, signals):
        return self._call("triage", signals)

    def decide_interrupt(self, spec, signals, ctx, *, history=None, review_feedback=None):
        return self._call(
            "decide_interrupt", spec, signals, ctx,
            history=history, review_feedback=review_feedback,
        )

    def summarize(self, ctx):
        return self._call("summarize", ctx)

    def verify(self, spec, ctx):
        return self._call("verify", spec, ctx)

    def review_decomposition(self, root_goal, specs):
        return self._call("review_decomposition", root_goal, specs)

    def review_spec_change(self, spec, signals, verdict):
        return self._call("review_spec_change", spec, signals, verdict)

    def decompose(self, root_goal, *, feedback=None, existing=None, environment=None):
        return self._call(
            "decompose", root_goal, feedback=feedback, existing=existing,
            environment=environment,
        )

    def profile_tasks(self, specs):
        return self._call("profile_tasks", specs)

    def probe(self, spec, ctx, excerpts):
        return self._call("probe", spec, ctx, excerpts)

    # -- 透传：不是模型调用，不花钱 ------------------------------------------ #

    def __getattr__(self, item):
        """没显式转发的东西一律透给内层。

        `cache_stats` 就是这么一个：它在真实后端上是**属性**（`CacheStats` 实例）
        而不是方法，把它当方法转发会让 `cli._report_cache` 拿到一个函数对象
        —— 实测踩过。用 `__getattr__` 兜住这一类「不是模型调用、只是想读点东西」
        的访问，比逐个猜要稳。

        注意它只在正常属性查找失败时才被调用，所以上面那些显式转发的方法
        不会走到这里 —— 花钱的路径仍然只有 `_call` 一条。
        """
        return getattr(self.inner, item)
