"""按任务把 Subagent 路由到不同供应商（§10.3.3）。

**为什么值得有**：不同的活儿适合不同的模型，而拆解之后每个子任务的活儿是明确的
（写后端 / 写前端 / 写文档 / 补测试）。让一个人在派发前把这件事定下来，比让所有
子任务共用一家更贴近实际。

**三条边界，破了就不是这个架构了**：

1. **只有 Subagent 会被路由。** 架构师（中断决策、验收、拆解、复核、分诊）永远走
   同一个后端 —— §2.3 说它是单一实例、持有连续上下文，按任务换供应商等于把
   「唯一写入决策点」拆成了几个。所以这里只有 `next_step()` 分发，其余全部转给
   `default`。
2. **模型不选模型。** 分配是人定的（`HumanGate.assign_models`），架构师只提供
   任务特点的描述。这和 `SpecTemplate` 不让模型给自己配沙箱是同一条原则：
   环境决策归调用方，不归被调用的那个。
3. **分配要落在 spec 上。** 写进 `TaskSpec.model`（`"供应商:模型"`），
   于是它会进存储、进界面、进 checkpoint —— 而不是只活在某个进程的内存里。
   重启之后还知道这个任务当初是谁在跑。

代价要说在前面：**跨供应商会让每家的前缀缓存各自冷启动**（§11.14 实测单家 74%），
而且 `policy.py` 那些参数全部出自单一供应商的跑批（§11.6），混用之后不能直接外推。
"""

from __future__ import annotations

from typing import Any

from ..types import TaskSpec

SEP = ":"


def split_model(model: str) -> tuple[str | None, str]:
    """`"kimi:kimi-k3"` → `("kimi", "kimi-k3")`；没有前缀就是 `(None, 原值)`。

    不带前缀的照旧走默认后端 —— 老的 spec、手写的 spec 都不用改。
    """
    if SEP in model:
        provider, _, name = model.partition(SEP)
        if provider and name:
            return provider, name
    return None, model


def assign_providers(
    specs: list[TaskSpec], assignment: dict[str, str], models: dict[str, str]
) -> list[TaskSpec]:
    """把「谁用哪家」写进 spec。

    `assignment`：task_id → 供应商名。没提到的任务保持原样。
    `models`：供应商名 → 那家的 Subagent 模型 id。

    **不 bump revision**：这发生在任何一次派发之前，不是「改任务」——
    revision 是给「规格变了、要重新开始」用的，在这里加一次会让后面每条
    DecisionRecord 的 revision 都错位一格。
    """
    out: list[TaskSpec] = []
    for spec in specs:
        provider = assignment.get(spec.id)
        if not provider:
            out.append(spec)
            continue
        model = models.get(provider) or split_model(spec.model)[1]
        out.append(spec.bump(model=f"{provider}{SEP}{model}", revision=spec.revision))
    return out


class RoutingBackend:
    """按 `TaskSpec.model` 的供应商前缀分发 `next_step()`，其余角色固定走 default。"""

    def __init__(
        self, default: Any, backends: dict[str, Any], *,
        subagent_default: str | None = None,
    ) -> None:
        self.default = default
        self.backends = dict(backends)
        # 「所有干活的都用这一家」（设置页的角色选择）。它是 spec 没写前缀时的
        # 落点，**不是覆盖** —— 单个任务上的按任务分配（§10.3.3）仍然更优先，
        # 那是人对着任务画像做的更细的决定。
        self.subagent_default = subagent_default
        # 用过哪几家 —— 跑完要能说清楚「这次实际是谁在干活」
        self.used: dict[str, int] = {}

    @property
    def name(self) -> str:
        inner = getattr(self.default, "name", "?")
        return f"routing[{inner} + {len(self.backends)} 家]"

    # -- 只有这一个方法会分发 ------------------------------------------------ #

    def next_step(self, ctx):
        provider, _ = split_model(ctx.task_spec.model)
        if provider is None:
            provider = self.subagent_default
        backend = self.backends.get(provider) if provider else None
        if backend is None:
            # 前缀不认识（配置里没这家 / 没写前缀）→ 回落默认，不报错。
            # 派发时因为一个模型名把整条链打挂是不划算的，而回落是可观测的：
            # `used` 会记在 "default" 名下。
            backend, provider = self.default, "default"
        self.used[provider] = self.used.get(provider, 0) + 1
        return backend.next_step(ctx)

    # -- 其余全部转给 default（架构师只有一个）-------------------------------- #

    def triage(self, signals):
        return self.default.triage(signals)

    def decide_interrupt(self, spec, signals, ctx, *, history=None, review_feedback=None):
        return self.default.decide_interrupt(
            spec, signals, ctx, history=history, review_feedback=review_feedback
        )

    def review_spec_change(self, spec, signals, verdict):
        return self.default.review_spec_change(spec, signals, verdict)

    def summarize(self, ctx):
        return self.default.summarize(ctx)

    def verify(self, spec, ctx):
        return self.default.verify(spec, ctx)

    def review_decomposition(self, root_goal, specs):
        return self.default.review_decomposition(root_goal, specs)

    def decompose(self, root_goal, *, feedback=None, existing=None):
        return self.default.decompose(
            root_goal, feedback=feedback, existing=existing
        )

    def profile_tasks(self, specs):
        return self.default.profile_tasks(specs)

    def probe(self, spec, ctx, excerpts):
        return self.default.probe(spec, ctx, excerpts)

    # -- 记账要能合并看 ------------------------------------------------------ #

    @property
    def cache_stats(self):
        """把各家的缓存记账并成一份。

        跨供应商的代价之一就是缓存各自冷启动，所以这里必须能一眼看出总命中率
        被摊薄成什么样 —— 分开看每家都「正常」，合起来才是真实成本。
        """
        from . import CacheStats

        total = CacheStats()
        for b in [self.default, *self.backends.values()]:
            s = getattr(b, "cache_stats", None)
            if s is None:
                continue
            total.calls += s.calls
            total.calls_with_usage += s.calls_with_usage
            total.prompt_tokens += s.prompt_tokens
            total.cached_tokens += s.cached_tokens
        return total
