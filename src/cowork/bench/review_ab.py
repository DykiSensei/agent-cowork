"""跨模型复核对照（§12 M7 7.2）。

这批实测回答的是整个 M7 的前提问题：**换一个供应商来复核，到底有没有用？**
前提不成立的话生成侧（7.3）就该换设计，所以它排在写生成者之前。

M5b 那个「10/10 正确、零假阳性」是**同模型**测出来的，不能外推（§11.10）。
跨模型复核多一种同模型没有的失败形态：复核者不共享拆解者的上下文，
**很可能对本来没问题的拆解报缺口** —— 代价是白跑一轮重生成或白打扰人一次。

所以两侧都测，口径抄 M5a（§11.9c）：

    正例 = 拆解确实有缺陷      召回率 TPR = 报出来的比例
    负例 = 拆解确实完整        假阳率 FPR = 误报的比例
    判别力 J = TPR - FPR       与同模型基线比，不与「看起来不错」比

**只测一侧一定会得出错误结论** —— M5a 第一版 ABANDON 判据在不可解任务上
12%→96% 看着是大胜，可解任务同时从 81% 塌到 56%。这里同样：一个逢拆解必报
缺口的复核者，正例侧是满分。

**这批数据回答不了的一个问题，先写在前面**：生成侧还不存在（7.3），用例表是手写的，
所以两个 arm 的差别只有「复核者是哪个模型」。它测的是**复核者的判别力**，
不是「独立于拆解者」这件事本身值多少 —— 后者要等生成侧上线，用「拆解者自查」对
「换一家复核」才测得出来。把这里的结论说成「独立复核有用」是过度解读。

三点设计约束，改这个文件前先看：

1. **负例必须真的完整**。负例里藏着可争议的缺口，测出来的「假阳性」其实是
   复核者对了，整份数据就废了。每个负例的 `note` 写清楚为什么算完整。
   **第一轮就栽在这**（§11.11）：`c_complete` 的两个 arm 十次全报缺口，读原文发现
   目标里的「一页」根本没有任何判据管 —— 复核者是对的，用例表是错的。
   写负例的方法因此定死：**把原始目标里的限定词逐个划出来，每个都要能指到一条判据**。
2. **缺陷不能只有一种形态**。§11.10 的局限之一就是只测了「整个子任务缺失」。
   这里三种：子任务缺失 / 验收标准太松 / 子任务之间的衔接没人验收。
   后两种是**结构检查抓不到**的，正是语义复核存在的理由。
3. **结构那一半要单独记**。`missing_subtask` 摘掉一支之后剩下的任务常常退化成
   一条链，`fan_out` 免费就叫了 —— 那不算语义复核的功劳，指标只算 `sufficient`。
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

from ..agent.architect import Architect
from ..policy import DEFAULT_POLICY
from ..store import SqliteStore
from ..types import Criterion, TaskClass, TaskSpec

# 复核上下文只渲染 id / depends_on / goal / acceptance（见 _render_review_context），
# task_class 和 sandbox 都不进提示词。所以 fixture 统一用 TOOL_CALL：
# 它不需要 sandbox，用例表因此是纯数据，不依赖临时目录。
_CLS = TaskClass.TOOL_CALL


def _spec(tid: str, goal: str, criteria: list[tuple[str, str]], *,
          deps: tuple[str, ...] = (), scope: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        id=tid,
        parent_id="task_review_root",
        goal=goal,
        acceptance=[Criterion(cid, desc) for cid, desc in criteria],
        task_class=_CLS,
        scope=list(scope or (f"{tid}.out",)),
        depends_on=list(deps),
    )


@dataclass(frozen=True)
class ReviewCase:
    """一个待复核的拆解，带标准答案。

    `complete=True` 是负例（期望 `sufficient=true`），`False` 是正例。
    `note` 是给读记录的人看的：负例写「为什么这算完整」，正例写「缺口是什么」——
    跨模型复核的争议全在这两句话上，没有它就没法判断一次假阳性是谁的问题。
    """

    id: str
    family: str
    root_goal: str
    specs: tuple[TaskSpec, ...]
    complete: bool
    defect: str
    note: str


# --------------------------------------------------------------------------- #
# 家族 A：报告小工具（沿用 M4 / M5b 的场景，好和 §11.10 的 10/10 直接对比）
# --------------------------------------------------------------------------- #

A_GOAL = (
    "做一个把 'name,42' 这样的文本行渲染成 'name = 42' 报告的小工具："
    "解析、格式化、组装成 render(lines) 接口，最后整体校验一遍。"
)

_a_parse = _spec(
    "t1_parse",
    "在 parse.py 里实现 parse_line(s)，把 'name,42' 解析成 ('name', 42)。",
    [("c1", "parse_line('a,1') 返回 ('a', 1)，parse_line('bb,22') 返回 ('bb', 22)")],
    scope=("parse.py",),
)
_a_format = _spec(
    "t2_format",
    "在 formatter.py 里实现 format_row(row)，把 ('a', 1) 渲染成 'a = 1'。",
    [("c1", "format_row(('a', 1)) 返回字符串 'a = 1'")],
    scope=("formatter.py",),
)
_a_report = _spec(
    "t3_report",
    "在 report.py 里实现 render(lines)：逐行调用 parse.parse_line，再用 "
    "formatter.format_row 渲染，最后用换行连接。",
    [("c1", "render(['a,1', 'bb,22']) 返回 'a = 1\\nbb = 22'")],
    deps=("t1_parse", "t2_format"),
    scope=("report.py",),
)
_a_check = _spec(
    "t4_check",
    "跑一遍全量校验，确认三个模块合起来工作正常。",
    [("c1", "verify_all.py 退出码为 0，且它逐个跑过 parse / format / report 三个校验")],
    deps=("t3_report",),
    scope=("checked.txt",),
)

FAMILY_A: tuple[ReviewCase, ...] = (
    ReviewCase(
        id="a_complete", family="report", root_goal=A_GOAL,
        specs=(_a_parse, _a_format, _a_report, _a_check),
        complete=True, defect="",
        note="解析 / 格式化 / 组装 / 整体校验四件事在目标里明写，四条验收标准逐一对应且都是行为判据",
    ),
    ReviewCase(
        id="a_missing", family="report", root_goal=A_GOAL,
        # 摘掉 t2 之后 t3 的依赖也要跟着摘，否则拓扑排序会直接报「依赖不存在」，
        # 那样测的是图检查而不是语义复核（同 demo_composite.build 的 drop 处理）
        specs=(_a_parse, _a_report.bump(depends_on=["t1_parse"]), _a_check),
        complete=False, defect="missing_subtask",
        note="没有任何子任务负责 formatter.format_row，目标里明写的「格式化」无人覆盖（§11.10 测的就是这一例）",
    ),
    ReviewCase(
        id="a_loose", family="report", root_goal=A_GOAL,
        specs=(
            _a_parse,
            _spec("t2_format",
                  "在 formatter.py 里实现 format_row(row)。",
                  [("c1", "formatter.py 存在，且 import formatter 不报错")],
                  scope=("formatter.py",)),
            _a_report, _a_check,
        ),
        complete=False, defect="loose_criterion",
        note="t2 的验收标准退化成存在性检查：format_row 渲染成什么样没有任何一条标准约束",
    ),
    ReviewCase(
        id="a_seam", family="report", root_goal=A_GOAL,
        specs=(
            _a_parse, _a_format,
            _spec("t3_report",
                  "在 report.py 里实现 render(rows)，把已经解析好的元组列表渲染成报告。",
                  [("c1", "render([('a', 1), ('bb', 22)]) 返回 'a = 1\\nbb = 22'")],
                  deps=("t1_parse", "t2_format"), scope=("report.py",)),
        ),
        complete=False, defect="uncovered_seam",
        note="三个部件各自验收都硬，但没有一条标准跨越 parse→format→render，"
             "「从文本行到报告」这条链和目标要求的「整体校验一遍」都没人验",
    ),
)


# --------------------------------------------------------------------------- #
# 家族 B：CSV 统计命令行工具（错误路径是目标的一部分）
# --------------------------------------------------------------------------- #

B_GOAL = (
    "写一个命令行工具 stats.py：读入一个带表头的 CSV 文件，对指定列算出 "
    "count / mean / max / min，结果以 JSON 打印到标准输出；"
    "输入文件不存在、或指定的列名不在表头里时，要打印明确的错误信息并以非零码退出。"
)

_b_reader = _spec(
    "b1_reader",
    "在 csvio.py 里实现 read_rows(path)，把带表头的 CSV 读成 dict 列表。",
    [("c1", "对一个 3 行 2 列的样例文件，read_rows 返回 3 个 dict，键为表头列名")],
    scope=("csvio.py",),
)
_b_stats = _spec(
    "b2_stats",
    "在 stats_core.py 里实现 summarize(rows, column)，返回 count / mean / max / min。",
    [("c1", "对 [{'x':'1'},{'x':'3'}] 求 x 列，返回 count=2 mean=2.0 max=3.0 min=1.0")],
    scope=("stats_core.py",),
)
_b_cli = _spec(
    "b3_cli",
    "在 stats.py 里实现命令行入口：接受文件路径与列名，调用上面两个模块，把结果 JSON 打到 stdout。",
    [("c1", "python stats.py sample.csv x 的 stdout 是合法 JSON，且四个字段的值与直接调用 summarize 一致")],
    deps=("b1_reader", "b2_stats"),
    scope=("stats.py",),
)
_b_errors = _spec(
    "b4_errors",
    "在 stats.py 里补上两条错误路径：文件不存在、列名不在表头里。",
    # c1 第一版只要求「退出码非 0 且 stderr 里出现该文件路径」—— 一个未捕获的
    # traceback 天然满足它，而目标要求的是「明确的错误信息」。两个复核者都指出了
    # 这一点（§11.11 的用例表返工），所以判据在这里补成「不是 traceback」。
    [("c1", "指向不存在的文件时退出码非 0，且 stderr 是一条说明『文件不存在』的"
            "单行错误信息（不是未捕获异常的 traceback），其中包含该文件路径"),
     ("c2", "列名不在表头里时退出码非 0，且 stderr 里说明该列名不存在并列出实际可用的列名")],
    deps=("b3_cli",),
    scope=("stats.py",),
)

FAMILY_B: tuple[ReviewCase, ...] = (
    ReviewCase(
        id="b_complete", family="csv_stats", root_goal=B_GOAL,
        specs=(_b_reader, _b_stats, _b_cli, _b_errors),
        complete=True, defect="",
        note="读入 / 四个统计量 / JSON 到 stdout / 两条错误路径都有行为判据；"
             "「明确的错误信息」这个限定词由 b4.c1 的『不是 traceback』兜住（返工后，见 §11.11）",
    ),
    ReviewCase(
        id="b_missing", family="csv_stats", root_goal=B_GOAL,
        specs=(_b_reader, _b_stats, _b_cli),
        complete=False, defect="missing_subtask",
        note="目标后半句明写的「明确报错 + 非零退出」没有任何子任务负责",
    ),
    ReviewCase(
        id="b_loose", family="csv_stats", root_goal=B_GOAL,
        specs=(
            _b_reader, _b_stats, _b_cli,
            _spec("b4_errors",
                  "在 stats.py 里补上错误处理。",
                  [("c1", "stats.py 里有 try / except，不会因为坏输入抛出未捕获异常")],
                  deps=("b3_cli",), scope=("stats.py",)),
        ),
        complete=False, defect="loose_criterion",
        note="「不抛未捕获异常」和目标要求的「明确报错 + 非零退出」不是一回事："
             "静默吞掉异常再返回 0 也能满足这条验收标准",
    ),
    ReviewCase(
        id="b_seam", family="csv_stats", root_goal=B_GOAL,
        specs=(
            _b_reader, _b_stats,
            _spec("b3_cli",
                  "在 stats.py 里实现命令行入口。",
                  [("c1", "python stats.py --help 退出码为 0 并打印用法说明")],
                  deps=("b1_reader", "b2_stats"), scope=("stats.py",)),
            _b_errors,
        ),
        complete=False, defect="uncovered_seam",
        note="reader 与 stats_core 各自验收都硬，但没有一条标准验「命令行跑完真的输出了 JSON 统计结果」，"
             "两个模块之间的衔接和目标的主输出都落空",
    ),
)


# --------------------------------------------------------------------------- #
# 家族 C：文档写作（非代码题 —— 避免结论只在 CODE 类任务上成立）
# --------------------------------------------------------------------------- #

C_GOAL = (
    "为内部的 signals 模块写一份使用文档：一页概念说明、一份把所有信号类型逐个列全的速查表、"
    "以及三个可以直接运行的示例；示例的输出必须和当前代码的实际行为一致。"
)

_c_outline = _spec(
    "c1_outline", "写 outline.md：给出文档的三段结构与每段要点。",
    [("c1", "outline.md 里概念说明 / 速查表 / 示例三节齐备，每节列出要点")],
    scope=("outline.md",),
)
_c_concept = _spec(
    "c2_concept", "写 concept.md：解释硬信号与软信号的区别、以及信号如何触发中断。",
    # c2 是用例表返工加的：目标里「一页」是个限定词，第一版没有任何判据管它，
    # 两个复核者五次里五次都点名了这一条（§11.11）。目标不动，补判据。
    [("c1", "concept.md 讲清硬/软信号的差别和各自的处理路径，且与 signals.py 的定义一致"),
     ("c2", "concept.md 正文不超过一页（不超过 60 行且不超过 3000 字符）")],
    deps=("c1_outline",), scope=("concept.md",),
)
_c_table = _spec(
    "c3_table", "写 table.md：逐个信号类型列出含义、级别、由谁产生。",
    [("c1", "table.md 的行集合与 SignalType 的成员集合完全一致，一个不多一个不少")],
    deps=("c1_outline",), scope=("table.md",),
)
_c_examples = _spec(
    "c4_examples", "写 examples.md 和 examples/ 下的三个脚本。",
    # c3 同样是返工加的：只验「能跑 + 输出与文档一致」的话，一个与 signals 无关的
    # 脚本也能通过 —— 目标要的是这个模块的使用示例。复核者原话见 §11.11。
    [("c1", "三个脚本都能直接运行且退出码为 0"),
     ("c2", "examples.md 里贴的输出与脚本实际输出逐字相同"),
     ("c3", "三个脚本都 import 了 signals 模块并调用其中的类型或函数，"
            "输出里出现真实的 SignalType 成员名")],
    deps=("c1_outline",), scope=("examples.md", "examples/*.py"),
)

FAMILY_C: tuple[ReviewCase, ...] = (
    ReviewCase(
        id="c_complete", family="docs", root_goal=C_GOAL,
        specs=(_c_outline, _c_concept, _c_table, _c_examples),
        complete=True, defect="",
        note="三段内容各有子任务，「列全」由集合相等判据覆盖，「与实际行为一致」由逐字比对覆盖；"
             "「一页」和「确实是 signals 的使用示例」两个限定词是返工后补的判据（见 §11.11）",
    ),
    ReviewCase(
        id="c_missing", family="docs", root_goal=C_GOAL,
        specs=(_c_outline, _c_concept, _c_table),
        complete=False, defect="missing_subtask",
        note="目标里明写的三个可运行示例没有任何子任务负责",
    ),
    ReviewCase(
        id="c_loose", family="docs", root_goal=C_GOAL,
        specs=(
            _c_outline, _c_concept,
            _spec("c3_table", "写 table.md：列出主要的信号类型。",
                  [("c1", "table.md 是一张至少 5 行的表格，每行有含义与级别两列")],
                  deps=("c1_outline",), scope=("table.md",)),
            _c_examples,
        ),
        complete=False, defect="loose_criterion",
        note="「至少 5 行」验不出目标要求的「所有信号类型逐个列全」，漏掉一半也能通过",
    ),
    ReviewCase(
        id="c_seam", family="docs", root_goal=C_GOAL,
        specs=(
            _c_outline, _c_concept, _c_table,
            _spec("c4_examples", "写 examples.md 和 examples/ 下的三个脚本。",
                  [("c1", "examples.md 里有三个代码块，每个都不少于 5 行")],
                  deps=("c1_outline",), scope=("examples.md", "examples/*.py")),
        ),
        complete=False, defect="uncovered_seam",
        note="示例存在但没人验它能不能跑、输出对不对，目标里「可以直接运行」「与实际行为一致」两个限定词全落空",
    ),
)


ALL_CASES: tuple[ReviewCase, ...] = FAMILY_A + FAMILY_B + FAMILY_C
BY_ID = {c.id: c for c in ALL_CASES}


def select_cases(only: str | None = None) -> list[ReviewCase]:
    """逗号分隔的用例 id、家族名或缺陷形态；不给就全跑。"""
    if not only:
        return list(ALL_CASES)
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    return [
        c for c in ALL_CASES
        if c.id in wanted or c.family in wanted or (c.defect or "complete") in wanted
    ]


# --------------------------------------------------------------------------- #
# 跑批
# --------------------------------------------------------------------------- #


@dataclass
class ReviewRecord:
    case_id: str
    family: str
    defect: str
    complete: bool
    arm: str
    independent: bool
    run_index: int
    reviewer: str = "?"
    sufficient: bool | None = None
    missing: list[str] = field(default_factory=list)
    structural: list[dict] = field(default_factory=list)
    tokens: int = 0
    wall_seconds: float = 0.0
    error: str = ""

    @property
    def structural_caught(self) -> bool:
        return bool(self.structural)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["structural_caught"] = self.structural_caught
        return d


def review_once(
    case: ReviewCase,
    *,
    arm: str,
    base_factory: Callable[[], Any],
    reviewer_factory: Callable[[], Any] | None,
    run_index: int,
) -> ReviewRecord:
    """跑一次复核。

    `reviewer_factory=None` 就是同模型基线（M5b 的形态），走的是
    `Architect.review_decomposition` 里 `reviewer_backend or backend` 的默认分支 ——
    **对照组和实验组共用同一段代码**，差别只有复核者是谁。
    """
    rec = ReviewRecord(
        case_id=case.id, family=case.family, defect=case.defect or "complete",
        complete=case.complete, arm=arm, independent=reviewer_factory is not None,
        run_index=run_index,
    )
    t0 = time.monotonic()
    try:
        architect = Architect(
            base_factory(), SqliteStore(), policy=DEFAULT_POLICY,
            reviewer_backend=reviewer_factory() if reviewer_factory else None,
        )
        review = architect.review_decomposition(case.root_goal, list(case.specs))
        rec.reviewer = review.reviewer
        rec.sufficient = review.sufficient
        rec.missing = list(review.missing)
        rec.structural = [
            {"kind": i.kind, "detail": i.detail, "tasks": list(i.tasks)}
            for i in review.structural
        ]
        rec.tokens = review.tokens
    except Exception:
        # 模型调用失败不能带塌整批：记下来，指标里单独扣掉
        rec.error = traceback.format_exc()[-1500:]
    rec.wall_seconds = round(time.monotonic() - t0, 3)
    return rec


def run_batch(
    cases: list[ReviewCase],
    *,
    base_factory: Callable[[], Any],
    arms: dict[str, Callable[[], Any] | None],
    repeat: int,
    out_path: Path,
    workers: int = 4,
    progress: Callable[[ReviewRecord, int, int], None] | None = None,
) -> list[ReviewRecord]:
    jobs = [
        (c, arm, factory, i)
        for c in cases
        for arm, factory in arms.items()
        for i in range(1, repeat + 1)
    ]
    total = len(jobs)
    records: list[ReviewRecord] = []

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    review_once, c, arm=arm, base_factory=base_factory,
                    reviewer_factory=factory, run_index=i,
                ): (c, arm, i)
                for c, arm, factory, i in jobs
            }
            for done, fut in enumerate(as_completed(futures), start=1):
                rec = fut.result()
                records.append(rec)
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                if progress:
                    progress(rec, done, total)
    return records


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #


def load(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _rate(hit: int, n: int) -> float | None:
    return round(hit / n, 4) if n else None


def arm_metrics(recs: list[dict]) -> dict[str, Any]:
    """按 arm 出 TPR / FPR / Youden J，口径与 §11.9c 的 M5a 表一致。

    **阳性判定是 `sufficient=false`**，不看 `missing` 里写了什么 —— 复核者报了
    缺口但报错了地方，机制上仍然会触发重生成，代价是一样的。判得准不准要人读
    `missing` 的原文，那是报告最后一段的事。

    出错的记录整条扣掉：把模型调用失败算成「没报缺口」会把 FN 做多，
    那是基础设施的账，不是判别力的账。
    """
    ok = [r for r in recs if not r["error"] and r["sufficient"] is not None]
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_arm[r["arm"]].append(r)

    out: dict[str, Any] = {}
    for arm, rs in by_arm.items():
        pos = [r for r in rs if not r["complete"]]
        neg = [r for r in rs if r["complete"]]
        tp = sum(1 for r in pos if r["sufficient"] is False)
        fp = sum(1 for r in neg if r["sufficient"] is False)
        tpr = _rate(tp, len(pos))
        fpr = _rate(fp, len(neg))
        tokens = [r["tokens"] for r in rs if r["tokens"]]
        out[arm] = {
            "runs": len(rs),
            "errors": sum(1 for r in recs if r["arm"] == arm and r["error"]),
            "independent": bool(rs[0]["independent"]),
            "reviewers": sorted({r["reviewer"] for r in rs}),
            "positives": len(pos), "negatives": len(neg),
            "tp": tp, "fn": len(pos) - tp, "fp": fp, "tn": len(neg) - fp,
            "tpr": tpr, "fpr": fpr,
            "youden_j": round(tpr - fpr, 4) if tpr is not None and fpr is not None else None,
            "recall_by_defect": {
                d: _rate(sum(1 for r in pos if r["defect"] == d and r["sufficient"] is False),
                         sum(1 for r in pos if r["defect"] == d))
                for d in sorted({r["defect"] for r in pos})
            },
            "fp_by_family": {
                f: _rate(sum(1 for r in neg if r["family"] == f and r["sufficient"] is False),
                         sum(1 for r in neg if r["family"] == f))
                for f in sorted({r["family"] for r in neg})
            },
            "tokens_median": round(statistics.median(tokens), 1) if tokens else 0,
            "seconds_median": round(
                statistics.median([r["wall_seconds"] for r in rs]), 2
            ),
        }
    return out


def free_half_credit(recs: list[dict]) -> dict[str, Any]:
    """正例里有多少是**结构检查免费抓到的** —— 这部分不该记在语义复核头上。

    `missing_subtask` 摘掉一支之后常常退化成一条链，`fan_out` 自己就叫了
    （§11.10 那个「顺带的收获」）。不扣掉它，跨模型复核的成绩会被结构检查垫高。
    """
    pos = [r for r in recs if not r["error"] and not r["complete"]]
    neg = [r for r in recs if not r["error"] and r["complete"]]
    by_defect: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "structural": 0})
    for r in pos:
        row = by_defect[r["defect"]]
        row["n"] += 1
        row["structural"] += 1 if r["structural_caught"] else 0
    return {
        "positives_flagged_by_structure": {
            d: f"{v['structural']}/{v['n']}" for d, v in sorted(by_defect.items())
        },
        # 负例上的结构告警是纯噪声：拆解是完整的，结构还叫，说明这条判据本身偏保守
        "negatives_flagged_by_structure": f"{sum(1 for r in neg if r['structural_caught'])}/{len(neg)}",
        "structural_kinds": dict(
            Counter(i["kind"] for r in pos + neg for i in r["structural"])
        ),
    }


def disagreements(recs: list[dict]) -> list[dict]:
    """同一个用例上各 arm 判得不一样的地方 —— 人要读的就是这些。"""
    by_case: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for r in recs:
        if not r["error"] and r["sufficient"] is not None:
            by_case[r["case_id"]][r["arm"]].append(bool(r["sufficient"]))

    out = []
    for case_id, arms in sorted(by_case.items()):
        verdicts = {a: sum(1 for x in v if not x) for a, v in arms.items()}  # 报缺口的次数
        counts = {a: len(v) for a, v in arms.items()}
        rates = {a: verdicts[a] / counts[a] for a in arms if counts[a]}
        if len(set(round(x, 2) for x in rates.values())) > 1:
            case = BY_ID.get(case_id)
            out.append({
                "case_id": case_id,
                "complete": case.complete if case else None,
                "defect": case.defect if case else "",
                "flag_rate_by_arm": {a: round(x, 2) for a, x in rates.items()},
                "note": case.note if case else "",
            })
    return out


def sample_findings(recs: list[dict], limit: int = 8) -> list[dict]:
    """负例上被报出来的缺口原文。

    **假阳性是不是真的假，只能人读这段。** 复核者不共享拆解者的上下文，
    它报的东西可能确实是个缺口 —— 那样的话该改的是负例，不是复核者。
    """
    out = []
    for r in recs:
        if r["error"] or not r["complete"] or r["sufficient"] is not False:
            continue
        out.append({"case_id": r["case_id"], "arm": r["arm"], "missing": r["missing"]})
        if len(out) >= limit:
            break
    return out


def summarize(recs: list[dict]) -> dict[str, Any]:
    return {
        "records": len(recs),
        "errors": sum(1 for r in recs if r["error"]),
        "cases": len({r["case_id"] for r in recs}),
        "arms": arm_metrics(recs),
        "free_half": free_half_credit(recs),
        "disagreements": disagreements(recs),
        "false_positive_findings": sample_findings(recs),
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        "=" * 74,
        "跨模型复核对照（§12 M7 7.2）",
        "=" * 74,
        f"记录 {summary['records']} 条，用例 {summary['cases']} 个，出错 {summary['errors']} 条",
        "",
        "阳性 = 拆解确实有缺陷且复核者报了 sufficient=false",
        f"{'arm':<14}{'独立':<6}{'runs':<6}{'TPR':<8}{'FPR':<8}{'J':<8}{'token中位':<10}",
        "-" * 74,
    ]
    for arm, m in sorted(summary["arms"].items()):
        lines.append(
            f"{arm:<14}{'是' if m['independent'] else '否':<6}{m['runs']:<6}"
            f"{_fmt(m['tpr']):<8}{_fmt(m['fpr']):<8}{_fmt(m['youden_j']):<8}"
            f"{m['tokens_median']:<10.0f}"
        )
        lines.append(
            f"{'':14}TP={m['tp']} FN={m['fn']} FP={m['fp']} TN={m['tn']}"
            f"  复核者={','.join(m['reviewers'])} 出错={m['errors']}"
        )
        lines.append(f"{'':14}按缺陷形态的召回：{m['recall_by_defect']}")
        lines.append(f"{'':14}按家族的假阳率：  {m['fp_by_family']}")
        lines.append("")

    free = summary["free_half"]
    lines += [
        "免费那一半（结构检查）单独记账 —— 这部分不算语义复核的功劳：",
        f"  正例被结构检查抓到  {free['positives_flagged_by_structure']}",
        f"  负例上的结构告警    {free['negatives_flagged_by_structure']}（应为 0，非 0 说明判据偏保守）",
        f"  告警类型分布        {free['structural_kinds']}",
        "",
    ]

    if summary["disagreements"]:
        lines.append("各 arm 判得不一样的用例（人要读的就是这些）：")
        for d in summary["disagreements"]:
            kind = "完整" if d["complete"] else f"缺陷={d['defect']}"
            lines.append(f"  {d['case_id']:<14}{kind:<24}报缺口率 {d['flag_rate_by_arm']}")
            lines.append(f"  {'':14}{d['note']}")
        lines.append("")

    if summary["false_positive_findings"]:
        lines.append("负例上被报出来的「缺口」原文 —— 假阳性是不是真的假，只能人读这段：")
        for f in summary["false_positive_findings"]:
            lines.append(f"  [{f['arm']}] {f['case_id']}: {'; '.join(f['missing'])[:300]}")
        lines.append("")

    lines += [
        "读法：",
        "  1. 这里测的是**复核者模型的判别力**，不是「独立性的收益」。生成侧还不存在",
        "     （7.3），拆解是手写的，所以两个 arm 的差别只有「谁来复核」这一项。",
        "     独立性本身值多少，要等生成侧上线后用『拆解者自查 vs 换一家复核』才测得出来。",
        "  2. 基线是 arm=拆解者同款模型（§11.10 的 10/10 就是它跑的）；",
        "  3. TPR 高但 FPR 也高 = 无差别报缺口，不是判别力 —— M5a 第一版就栽在这（§11.9c）；",
        "  4. 每个 arm 的 runs 不足 20 时，两侧率值的置信区间宽到没法比，先加 --repeat。",
    ]
    return "\n".join(lines)


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"
