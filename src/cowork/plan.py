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


def deterministic_review(root_goal: str, specs: list[TaskSpec]) -> list[PlanIssue]:
    """拆解复核的免费那一半（§12 M5b）。

    这些检查不需要模型，也因此**不会漏判自己**——它们查的是结构性缺陷：
    子任务写不了任何东西、依赖悬空、有环、拆了等于没拆。
    模型那一半（`Backend.review_decomposition`）查的是语义缺陷：
    「满足这些验收标准是不是就等于完成了原始目标」，那个必须问模型。

    先跑这一半：结构坏了就没必要花 token 问语义。
    """
    issues: list[PlanIssue] = []
    if not specs:
        return [PlanIssue("empty", "拆解产出为空", ())]

    for s in specs:
        if not s.scope:
            issues.append(
                PlanIssue("no_scope", f"子任务 {s.id} 没有 scope，它写不了任何东西", (s.id,))
            )
        if not s.acceptance:
            # TaskSpec.__post_init__ 本来就拦，这里是双保险：拆解产出若绕过构造
            issues.append(
                PlanIssue("no_acceptance", f"子任务 {s.id} 没有验收标准（§4.1 硬约束）", (s.id,))
            )
        if not s.goal.strip():
            issues.append(PlanIssue("empty_goal", f"子任务 {s.id} 的 goal 为空", (s.id,)))

    issues.extend(_isolated_dependencies(specs))

    try:
        plan = build_plan(specs)
    except PlanError as exc:
        issues.append(PlanIssue("invalid_graph", str(exc), tuple(s.id for s in specs)))
        return issues

    issues.extend(plan.issues)
    return issues


def _isolated_dependencies(specs: list[TaskSpec]) -> list[PlanIssue]:
    """有依赖关系、产出却各在各的目录 —— 拼起来大概率 import 不到（§12 M7 7.3）。

    这条是被真实生成的拆解逼出来的（§11.12）：模型知道「scope 不能相交」是硬要求，
    于是用**一人一个目录**来满足它 —— `subtask1/csv_parser.py`、
    `subtask2/markdown_renderer.py`、`subtask3/cli.py`，而 cli 依赖前两个。
    scope 确实不相交了，`python subtask3/cli.py` 也确实 import 不到那两个模块。

    两层复核都看不见它：结构层只查交集与环，语义层问的是「验收标准合起来等不等于
    原始目标」。**它属于第三个问题 —— 拆出来的东西合起来能不能跑。**

    判据刻意收窄，只报「A 依赖 B，且两边的产出全部落在各自不同的子目录里」：
    根目录、共享目录、一方在根一方在子目录都不报 —— 那些都有正常的组织方式。
    """
    by_id = {s.id: s for s in specs}
    out: list[PlanIssue] = []
    for spec in specs:
        mine = _sole_dir(spec)
        if mine is None:
            continue
        for dep_id in spec.depends_on:
            dep = by_id.get(dep_id)
            if dep is None:
                continue  # 悬空依赖归 invalid_graph 管
            theirs = _sole_dir(dep)
            if theirs is not None and theirs != mine:
                out.append(
                    PlanIssue(
                        kind="isolated_dependency",
                        detail=(
                            f"{spec.id} 依赖 {dep.id}，但产出分别在 {mine}/ 和 {theirs}/ —— "
                            "各自一个目录能满足 scope 不相交，代价是运行时互相 import 不到。"
                            "修法：别一人一个目录。把共享的入口/公共模块放到根目录或同一个 "
                            "src/ 目录（scope 按文件级不相交即可），依赖方从那里 import；"
                            "或者把这条依赖改成顺序链，由依赖链末端的集成子任务收口"
                        ),
                        tasks=(spec.id, dep.id),
                    )
                )
    return out


def _sole_dir(spec: TaskSpec) -> str | None:
    """这个任务的产出是不是全关在同一个子目录里；否则 None。

    有任何一个产出在根目录，就不算 —— 那时候依赖方从根目录 import 得到。
    """
    if not spec.scope or not all("/" in p.replace("\\", "/") for p in spec.scope):
        return None
    dirs = {p.replace("\\", "/").rsplit("/", 1)[0] for p in spec.scope}
    return next(iter(dirs)) if len(dirs) == 1 else None


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
