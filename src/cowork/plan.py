"""任务图：拓扑分层、可分解性评估、静态冲突检测（§12 M4 的 4.2 / 4.3）。

**这个模块里没有 LLM，也不该有**。它回答的全部是确定性问题：谁依赖谁、
哪些任务能同时跑、哪两个任务会写同一个文件。架构师负责「拆成什么」，
这里只负责「拆出来的东西能不能并行、并行安不安全」。

§1.4 第三条设计前提是这里的立法依据：**顺序依赖强的任务，多 agent 相对单 agent
最差 −70%**。所以可分解性评估不是锦上添花——评估结果是「不可分解」时，
正确做法是退化为串行，而不是硬并行。宁可慢，不要 −70%。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .types import TaskSpec


class PlanError(ValueError):
    """计划本身无效：有环、依赖不存在。这类错误不该进执行层。"""


@dataclass(frozen=True)
class PlanIssue:
    kind: str          # scope_overlap | serialized | fan_out
    detail: str
    tasks: tuple[str, ...]


@dataclass
class Plan:
    layers: list[list[TaskSpec]]
    issues: list[PlanIssue] = field(default_factory=list)

    @property
    def max_parallel(self) -> int:
        return max((len(x) for x in self.layers), default=0)

    @property
    def decomposable(self) -> bool:
        """有没有真正的并行度。全是单任务层 = 拆了等于没拆（§1.4 第三条）。"""
        return self.max_parallel > 1

    def to_dict(self) -> dict:
        return {
            "layers": [[t.id for t in layer] for layer in self.layers],
            "max_parallel": self.max_parallel,
            "decomposable": self.decomposable,
            "issues": [
                {"kind": i.kind, "detail": i.detail, "tasks": list(i.tasks)}
                for i in self.issues
            ],
        }


# --------------------------------------------------------------------------- #


def topo_layers(specs: list[TaskSpec]) -> list[list[TaskSpec]]:
    """按 depends_on 分层。同一层内的任务互不依赖，可以并行。

    Kahn 算法。发现环就抛 —— 环意味着拆解本身错了，不能靠运行时兜底。
    """
    by_id = {s.id: s for s in specs}
    if len(by_id) != len(specs):
        raise PlanError("任务 id 重复")

    for s in specs:
        for d in s.depends_on:
            if d not in by_id:
                raise PlanError(f"任务 {s.id} 依赖了不存在的任务 {d}")
    remaining = {s.id: set(s.depends_on) for s in specs}

    layers: list[list[TaskSpec]] = []
    done: set[str] = set()
    while remaining:
        ready = [tid for tid, deps in remaining.items() if deps <= done]
        if not ready:
            raise PlanError(f"depends_on 里有环，涉及: {sorted(remaining)}")
        # 排序让分层结果可复现 —— 实测要能重跑
        ready.sort()
        layers.append([by_id[t] for t in ready])
        done.update(ready)
        for t in ready:
            del remaining[t]
    return layers


def scope_overlaps(specs: list[TaskSpec]) -> list[tuple[str, str, list[str]]]:
    """两两检查 scope 有没有交集。返回 (任务A, 任务B, 冲突模式)。

    这是**派发之前**就能做的确定性检查，比等 `CONFLICT_SUSPECTED` 软信号
    可靠得多——§3.1 已经确立软信号靠不住，冲突检测不能只依赖 Subagent 自报。

    交集判定对两边都做 fnmatch：`solution.py` 与 `*.py` 算冲突，
    `a/*.py` 与 `a/b.py` 也算。宁可误报——误报的代价是串行，漏报的代价是覆盖产出。
    """
    out: list[tuple[str, str, list[str]]] = []
    for i, a in enumerate(specs):
        for b in specs[i + 1:]:
            hits = sorted(
                {p for p in a.scope for q in b.scope if _patterns_overlap(p, q)}
                | {q for p in a.scope for q in b.scope if _patterns_overlap(p, q)}
            )
            if hits:
                out.append((a.id, b.id, hits))
    return out


def _patterns_overlap(p: str, q: str) -> bool:
    return p == q or fnmatch.fnmatch(p, q) or fnmatch.fnmatch(q, p)


def build_plan(specs: list[TaskSpec]) -> Plan:
    """拓扑分层 + 把有 scope 交集的并行任务串行化。

    串行化是保守的正确行为：并行写同一个文件的结果是「谁后写谁赢」，
    而那是**静默的**——没有任何信号会告诉你产出被覆盖了。
    """
    layers = topo_layers(specs)
    issues: list[PlanIssue] = []

    final: list[list[TaskSpec]] = []
    for layer in layers:
        overlaps = scope_overlaps(layer) if len(layer) > 1 else []
        if not overlaps:
            final.append(layer)
            continue
        for a, b, hits in overlaps:
            issues.append(
                PlanIssue(
                    kind="scope_overlap",
                    detail=f"scope 交集 {hits}，并行会静默覆盖产出",
                    tasks=(a, b),
                )
            )
        # 有交集就整层串行 —— 不做「部分并行」的聪明优化：
        # 那需要求最大独立集，收益不确定，而错了的代价是静默覆盖。
        issues.append(
            PlanIssue(
                kind="serialized",
                detail=f"该层 {len(layer)} 个任务因 scope 冲突改为串行",
                tasks=tuple(t.id for t in layer),
            )
        )
        final.extend([t] for t in layer)

    plan = Plan(layers=final, issues=issues)
    if not plan.decomposable and len(specs) > 1:
        issues.append(
            PlanIssue(
                kind="fan_out",
                detail=(
                    "没有任何一层能并行，拆解没带来并行度。§1.4 第三条："
                    "顺序依赖强的任务多 agent 最差 −70%，这种情况应退化为单 agent"
                ),
                tasks=tuple(s.id for s in specs),
            )
        )
    return plan
