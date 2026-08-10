"""生成-复核循环的真实样本 + 拆解提示词对照（§12 M7 7.4 的补课，风险 #17）。

**这批实测要解决的是一个空白，不是一个参数**：M7 收口时 5 个目标全部一轮通过复核，
于是「重生成」这条路径在真实模型上一次都没跑过 —— 循环、两条上限、复核意见回喂
全部只有脚本后端的测试覆盖。`max_regenerate=2` 的依据也因此是结构性的
（跟 `max_rebase` 同构），不是实测的。

**「测试全绿」和「这条路径被真实跑过」是两件事。**

怎么才能拿到样本：不能靠等 —— 生成者拆得好的时候复核者就是会放行。所以做成对照，
把「被驳回」当成实验条件而不是意外：

    full   现在的 DECOMPOSE_SYSTEM（带 §11.11 的限定词纪律）
    naive  没学过那条教训的朴素提示词 —— 只讲结构要求

naive 臂被驳回时产出的就是重生成样本。**同一批数据顺带回答第二个问题**：
限定词纪律到底值多少？这是 §11.9c 那条纪律的直接应用 —— 改任何影响质量的提示词，
都要有对照组，否则「看起来更好」就是错觉。

naive 不是稻草人：它仍然要求 scope 不相交、依赖不成环、每个子任务有验收标准 ——
那些是**装配层的硬约束**（`TaskSpec.__post_init__` 和 `deterministic_review`
本来就会拦），不给的话测的就是 schema 合规而不是拆解质量。它缺的只有那条
「把限定词逐个划出来 / 写行为不写存在性 / 衔接也要有人验收」的纪律。
"""

from __future__ import annotations

import json
import statistics
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agent.architect import Architect, SpecTemplate
from ..policy import DEFAULT_POLICY, Policy
from ..store import SqliteStore
from ..types import SandboxProfile

# 对照臂：把 §11.11 学到的东西全部拿掉，只留装配层本来就会拦的硬约束。
NAIVE_DECOMPOSE_SYSTEM = """你在把一个目标拆成若干可以独立派发的子任务。

每个子任务要给出：goal、验收标准、scope（它被允许写的文件）、depends_on。

结构上的硬要求：

1. 两个子任务的 scope 不能相交；
2. depends_on 只能引用本次拆解里的其它 id，不能有环；
3. 至少要有两个子任务能同时开跑；
4. 粒度：2~6 个子任务。

如果上面给了你上一轮复核发现的缺口，针对每一条改掉。"""


@dataclass(frozen=True)
class PlanGoal:
    """一个待拆解的目标。

    `limiters` 是我们人工数出来的限定词 —— 它**不参与判定**，只是让读记录的人
    能对着看复核者报的缺口是不是这些。判定权仍然只在复核者手上，
    否则就成了「拿我们自己的答案给模型打分」。
    """

    id: str
    goal: str
    limiters: tuple[str, ...]


GOALS: tuple[PlanGoal, ...] = (
    PlanGoal(
        "wc", "做一个命令行工具 wc.py：统计一个文本文件的行数、词数、字符数，"
              "以 JSON 打到 stdout；文件不存在时要打印明确的错误信息并以非零码退出。",
        ("JSON 到 stdout", "文件不存在→明确错误", "非零退出码"),
    ),
    PlanGoal(
        "csv2md", "做一个把 CSV 转成 Markdown 表格的小工具：支持带引号和逗号的字段，"
                  "第一行当表头，输出的表格在 GitHub 上要能正确渲染，空文件要给出提示而不是崩溃。",
        ("带引号/逗号的字段", "第一行为表头", "GitHub 可渲染", "空文件不崩溃"),
    ),
    PlanGoal(
        "logstats", "做一个日志分析工具：读 nginx access log，按状态码和 URL 聚合，"
                    "输出最慢的 10 个请求；要支持 gzip 输入，处理 1GB 文件时常驻内存不超过 200MB，"
                    "时间戳按本地时区解析。",
        ("gzip 输入", "内存 ≤200MB", "本地时区", "top 10 最慢"),
    ),
    PlanGoal(
        "retry", "写一个带重试的 HTTP 客户端封装：对 5xx 和连接超时重试，最多 3 次，"
                 "退避是指数的且带抖动，4xx 一律不重试，每次重试要打一条包含尝试次数的日志，"
                 "总耗时不超过 30 秒。",
        ("只重试 5xx/超时", "最多 3 次", "指数退避+抖动", "4xx 不重试", "日志含次数", "总耗时上限"),
    ),
    PlanGoal(
        "config", "做一个配置加载器：支持 YAML 文件和环境变量两个来源，环境变量优先，"
                  "缺少必填项时要报出缺的是哪一项而不是抛 KeyError，"
                  "布尔值要认 'true'/'1'/'yes' 三种写法，加载后的配置对象不可变。",
        ("两个来源", "环境变量优先", "缺项要指名", "三种布尔写法", "不可变"),
    ),
    PlanGoal(
        "dedup", "做一个去重工具：把一个目录下所有 .txt 文件里的重复行去掉并合并成一个文件，"
                 "保持首次出现的顺序，忽略行尾空白但区分大小写，"
                 "遇到编码不是 UTF-8 的文件要跳过并在最后汇总跳过了哪些。",
        ("保持首现顺序", "忽略行尾空白", "区分大小写", "非 UTF-8 跳过并汇总"),
    ),
)

BY_ID = {g.id: g for g in GOALS}


def select_goals(only: str | None = None) -> list[PlanGoal]:
    if not only:
        return list(GOALS)
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    return [g for g in GOALS if g.id in wanted]


# --------------------------------------------------------------------------- #
# 跑批
# --------------------------------------------------------------------------- #


@dataclass
class PlanRecord:
    goal_id: str
    arm: str
    run_index: int
    status: str = "ERROR"
    attempts: int = 0
    tokens: int = 0
    wall_seconds: float = 0.0
    subtasks: int = 0
    max_parallel: int = 0
    escalation_reason: str = ""
    escalation_kind: str = "none"       # none | repeat | cap | model_failure
    first_round_clean: bool = False
    recovered: bool = False             # 第一轮被驳回，后面某一轮通过了
    findings_per_round: list[list[str]] = field(default_factory=list)
    structural_per_round: list[list[str]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _escalation_kind(reason: str) -> str:
    if not reason:
        return "none"
    if "没有改变现实" in reason:
        return "repeat"
    if "max_regenerate" in reason:
        return "cap"
    if "无法产出合规拆解" in reason:
        return "model_failure"
    return "other"


def plan_once(
    goal: PlanGoal,
    *,
    arm: str,
    backend_factory: Callable[[], Any],
    reviewer_factory: Callable[[], Any] | None,
    run_index: int,
    policy: Policy = DEFAULT_POLICY,
    workspace_root: Path | None = None,
) -> PlanRecord:
    """跑一次完整的 plan()。

    **不给 human_gate**：升级时要落在 AWAITING_HUMAN 并保留 escalation_reason。
    挂个 AutoApproveGate 会把每一次升级都变成 ACCEPTED，那样正好把这次要测的
    东西抹掉 —— 我们要看的就是「循环自己停在哪里」。
    """
    rec = PlanRecord(goal_id=goal.id, arm=arm, run_index=run_index)
    t0 = time.monotonic()
    try:
        root = workspace_root or Path(".")
        ws = root / f"{goal.id}-{arm}-{run_index}"
        ws.mkdir(parents=True, exist_ok=True)
        architect = Architect(
            backend_factory(), SqliteStore(), policy=policy,
            reviewer_backend=reviewer_factory() if reviewer_factory else None,
        )
        result = architect.plan(
            goal.goal,
            SpecTemplate(sandbox=SandboxProfile(workspace=str(ws), allowed_binaries=("python",))),
        )
        rec.status = result.status
        rec.attempts = result.attempts
        rec.tokens = result.tokens
        rec.subtasks = len(result.specs)
        rec.escalation_reason = result.escalation_reason or ""
        rec.escalation_kind = _escalation_kind(rec.escalation_reason)
        rec.findings_per_round = [h["missing"] for h in result.history]
        rec.structural_per_round = [h["structural"] for h in result.history]
        rec.first_round_clean = bool(result.history) and result.history[0]["clean"]
        rec.recovered = (
            not rec.first_round_clean
            and result.status == "ACCEPTED"
            and len(result.history) > 1
        )
        if result.specs:
            from ..plan import build_plan

            try:
                rec.max_parallel = build_plan(result.specs).max_parallel
            except Exception:  # noqa: BLE001 - 图有问题时并行度无意义，留 0
                rec.max_parallel = 0
    except Exception:
        rec.error = traceback.format_exc()[-1500:]
    rec.wall_seconds = round(time.monotonic() - t0, 2)
    return rec


def run_batch(
    goals: list[PlanGoal],
    *,
    arms: dict[str, Callable[[], Any]],
    reviewer_factory: Callable[[], Any] | None,
    repeat: int,
    out_path: Path,
    workspace_root: Path | None = None,
    workers: int = 3,
    progress: Callable[[PlanRecord, int, int], None] | None = None,
) -> list[PlanRecord]:
    jobs = [
        (g, arm, factory, i)
        for g in goals
        for arm, factory in arms.items()
        for i in range(1, repeat + 1)
    ]
    records: list[PlanRecord] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    plan_once, g, arm=arm, backend_factory=factory,
                    reviewer_factory=reviewer_factory, run_index=i,
                    workspace_root=workspace_root,
                )
                for g, arm, factory, i in jobs
            ]
            for done, fut in enumerate(as_completed(futures), start=1):
                rec = fut.result()
                records.append(rec)
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                if progress:
                    progress(rec, done, len(jobs))
    return records


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #


def load(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def arm_summary(recs: list[dict]) -> dict[str, Any]:
    """每个臂的三组数：质量、循环、成本。

    **循环那组才是风险 #17 的正题** —— 第一轮就通过的运行对它没有贡献。
    """
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if not r["error"]:
            by_arm[r["arm"]].append(r)

    out: dict[str, Any] = {}
    for arm, rs in by_arm.items():
        rejected = [r for r in rs if not r["first_round_clean"]]
        out[arm] = {
            "runs": len(rs),
            "errors": sum(1 for r in recs if r["arm"] == arm and r["error"]),
            # 质量：第一轮就通过复核的比例
            "first_round_pass": round(
                sum(1 for r in rs if r["first_round_clean"]) / len(rs), 3) if rs else None,
            "accepted": round(
                sum(1 for r in rs if r["status"] == "ACCEPTED") / len(rs), 3) if rs else None,
            "status": dict(Counter(r["status"] for r in rs)),
            # 循环：被驳回之后发生了什么
            "rejected_runs": len(rejected),
            "recovered": sum(1 for r in rejected if r["recovered"]),
            "recovery_rate": round(
                sum(1 for r in rejected if r["recovered"]) / len(rejected), 3
            ) if rejected else None,
            "attempts": dict(Counter(r["attempts"] for r in rs)),
            "escalation_kind": dict(Counter(r["escalation_kind"] for r in rs if r["escalation_kind"] != "none")),
            # 成本
            "tokens_median": round(statistics.median([r["tokens"] for r in rs]), 0) if rs else 0,
            "seconds_median": round(statistics.median([r["wall_seconds"] for r in rs]), 1) if rs else 0,
            "subtasks_median": statistics.median([r["subtasks"] for r in rs if r["subtasks"]]) if any(r["subtasks"] for r in rs) else 0,
        }
    return out


def loop_evidence(recs: list[dict]) -> dict[str, Any]:
    """重生成路径到底被跑到了没有 —— 风险 #17 要的就是这一段。"""
    ok = [r for r in recs if not r["error"]]
    multi = [r for r in ok if r["attempts"] > 1]
    return {
        "runs_with_regeneration": len(multi),
        "max_attempts_seen": max((r["attempts"] for r in ok), default=0),
        "escalated_runs": sum(1 for r in ok if r["status"] == "AWAITING_HUMAN"),
        "escalation_kind": dict(Counter(r["escalation_kind"] for r in ok if r["escalation_kind"] != "none")),
        # 第二轮的缺口和第一轮一样不一样 —— 一样就说明重生成没改变现实
        "second_round_changed": sum(
            1 for r in multi
            if len(r["findings_per_round"]) > 1
            and r["findings_per_round"][0] != r["findings_per_round"][1]
        ),
    }


def finding_themes(recs: list[dict], limit: int = 12) -> list[dict]:
    """第一轮被报出来的缺口原文，按臂分组 —— 人要读的是这些。"""
    out = []
    for r in recs:
        if r["error"] or r["first_round_clean"] or not r["findings_per_round"]:
            continue
        out.append({
            "goal_id": r["goal_id"], "arm": r["arm"],
            "missing": r["findings_per_round"][0],
            "structural": r["structural_per_round"][0] if r["structural_per_round"] else [],
        })
        if len(out) >= limit:
            break
    return out


def summarize(recs: list[dict]) -> dict[str, Any]:
    return {
        "records": len(recs),
        "errors": sum(1 for r in recs if r["error"]),
        "goals": len({r["goal_id"] for r in recs}),
        "arms": arm_summary(recs),
        "loop": loop_evidence(recs),
        "first_round_findings": finding_themes(recs),
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        "=" * 74,
        "拆解提示词对照 + 生成-复核循环实测（§12 M7 7.4 / 风险 #17）",
        "=" * 74,
        f"记录 {summary['records']} 条，目标 {summary['goals']} 个，出错 {summary['errors']} 条",
        "",
        f"{'arm':<8}{'runs':<6}{'一轮过':<8}{'最终通过':<10}{'被驳回':<8}{'救回来':<8}"
        f"{'token中位':<10}{'子任务':<7}",
        "-" * 74,
    ]
    for arm, m in sorted(summary["arms"].items()):
        lines.append(
            f"{arm:<8}{m['runs']:<6}{_pct(m['first_round_pass']):<8}{_pct(m['accepted']):<10}"
            f"{m['rejected_runs']:<8}{m['recovered']:<8}{m['tokens_median']:<10.0f}"
            f"{m['subtasks_median']:<7}"
        )
        lines.append(f"{'':8}终局 {m['status']}  轮次分布 {m['attempts']}")
        if m["escalation_kind"]:
            lines.append(f"{'':8}升级原因 {m['escalation_kind']}")
        lines.append("")

    loop = summary["loop"]
    lines += [
        "重生成路径的证据（风险 #17 要的就是这一段）：",
        f"  真的跑了 ≥2 轮的运行    {loop['runs_with_regeneration']}",
        f"  见过的最大轮次          {loop['max_attempts_seen']}",
        f"  升级给人的运行          {loop['escalated_runs']}  原因 {loop['escalation_kind']}",
        f"  第二轮缺口与第一轮不同  {loop['second_round_changed']} / {loop['runs_with_regeneration']}"
        "（相同 = 重生成没改变现实，会被指纹判据抓住）",
        "",
    ]

    if summary["first_round_findings"]:
        lines.append("第一轮被报出来的缺口原文：")
        for f in summary["first_round_findings"]:
            head = f"  [{f['arm']}] {f['goal_id']}"
            if f["structural"]:
                lines.append(f"{head} 结构 {f['structural']}")
            for m in f["missing"]:
                lines.append(f"{head} {m[:220]}")
        lines.append("")

    lines += [
        "读法：",
        "  1. 两个臂的差 = 那条限定词纪律值多少。差不显著的话，提示词里那一大段就该删；",
        "  2. 「被驳回 → 救回来」的比例是重生成本身的价值 —— 低的话 max_regenerate 该收到 1；",
        "  3. 升级原因里 repeat（指纹重复）多于 cap，说明确定性判据在起作用而不是靠烧满上限。",
    ]
    return "\n".join(lines)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"
