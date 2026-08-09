"""复合任务演示（§12 M4 出口）：4 个子任务，2 种 task_class，最大并行度 2。

```
层1  t1_parse (CODE)  ‖  t2_format (CODE)      scope 不相交，真并行
层2  t3_report (CODE)                          depends_on 两者，拿到只读上下文
层3  t4_check (TOOL_CALL)                       跑全量校验
```

这个场景要展示的三件事，都是 M4 的出口标准：

1. **并行度来自调度层，不是通信层**。t1 / t2 同时跑，但它们之间没有任何 API 面 ——
   t3 能看到两者的产出，是因为调度器把 artifact 作为只读上下文注入（§8），
   不是因为 t1 和 t2 互相说话（§1.4 第一条）。
2. **拆解的可分解性是确定性算出来的**，不是模型说了算（`plan.build_plan()`）。
3. **冲突检测在产出层**，且只判同层并行写 —— 跨层写同一个文件是有序交接。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .actions import AgentAction, Finish, ToolCall
from .agent.architect import AutoApproveGate
from .llm import ArchitectVerdict, Triage
from .scheduler import Scheduler
from .store import SqliteStore
from .types import AgentContext, Criterion, SandboxProfile, Signal, TaskClass, TaskSpec

VERIFY_PARSE = '''\
import sys
from parse import parse_line
cases = [("a,1", ("a", 1)), ("bb,22", ("bb", 22))]
for arg, want in cases:
    got = parse_line(arg)
    if got != want:
        print(f"FAIL: parse_line({arg!r}) -> {got!r}, expected {want!r}", file=sys.stderr)
        sys.exit(1)
print("OK parse")
'''

VERIFY_FORMAT = '''\
import sys
from formatter import format_row
got = format_row(("a", 1))
if got != "a = 1":
    print(f"FAIL: format_row -> {got!r}, expected 'a = 1'", file=sys.stderr)
    sys.exit(1)
print("OK format")
'''

VERIFY_REPORT = '''\
import sys
from report import render
got = render(["a,1", "bb,22"])
want = "a = 1\\nbb = 22"
if got != want:
    print(f"FAIL: render -> {got!r}, expected {want!r}", file=sys.stderr)
    sys.exit(1)
print("OK report")
'''

VERIFY_ALL = '''\
import subprocess, sys
for script in ("verify_parse.py", "verify_format.py", "verify_report.py"):
    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"FAIL {script}: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
print("OK all")
'''

_SOLUTIONS = {
    "parse.py": (
        "def parse_line(s: str):\n"
        "    name, num = s.split(',')\n"
        "    return (name, int(num))\n"
    ),
    "formatter.py": "def format_row(row) -> str:\n    return f\"{row[0]} = {row[1]}\"\n",
    "report.py": (
        "from parse import parse_line\n"
        "from formatter import format_row\n\n"
        "def render(lines):\n"
        "    return '\\n'.join(format_row(parse_line(x)) for x in lines)\n"
    ),
    "checked.txt": "all green\n",
}


def build_workspace(root: str | None = None) -> Path:
    ws = Path(root or tempfile.mkdtemp(prefix="cowork-composite-"))
    ws.mkdir(parents=True, exist_ok=True)
    # 校验脚本都在 scope 外 —— 改它们来让测试通过会撞 SCOPE_VIOLATION
    (ws / "verify_parse.py").write_text(VERIFY_PARSE, encoding="utf-8")
    (ws / "verify_format.py").write_text(VERIFY_FORMAT, encoding="utf-8")
    (ws / "verify_report.py").write_text(VERIFY_REPORT, encoding="utf-8")
    (ws / "verify_all.py").write_text(VERIFY_ALL, encoding="utf-8")
    return ws


def _spec(ws: Path, tid: str, goal: str, *, produces: str, verify: str,
          deps=(), cls=TaskClass.CODE) -> TaskSpec:
    return TaskSpec(
        id=tid,
        parent_id="task_composite_root",
        goal=goal,
        acceptance=[Criterion("c1", f"{verify} 通过", ["python", verify])],
        task_class=cls,
        output_schema={
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
        sandbox=SandboxProfile(workspace=str(ws), allowed_binaries=("python",)),
        scope=[produces],
        tools=["write_file", "read_file", "list_files", "run"],
        depends_on=list(deps),
        max_steps=8,
        deadline_s=300.0,
        token_budget=60_000,
    )


def build_specs(ws: Path) -> list[TaskSpec]:
    return [
        _spec(ws, "t1_parse",
              "在 parse.py 里实现 parse_line(s)，把 'name,42' 解析成 ('name', 42)。",
              produces="parse.py", verify="verify_parse.py"),
        _spec(ws, "t2_format",
              "在 formatter.py 里实现 format_row(row)，把 ('a', 1) 渲染成 'a = 1'。",
              produces="formatter.py", verify="verify_format.py"),
        _spec(ws, "t3_report",
              "在 report.py 里实现 render(lines)：对每行调用 parse.parse_line，"
              "再用 formatter.format_row 渲染，最后用换行连接。",
              produces="report.py", verify="verify_report.py",
              deps=["t1_parse", "t2_format"]),
        _spec(ws, "t4_check",
              "跑一遍 verify_all.py 确认三个模块都好了，然后把 'all green' 写进 checked.txt。",
              produces="checked.txt", verify="verify_all.py",
              deps=["t3_report"], cls=TaskClass.TOOL_CALL),
    ]


class ScriptedComposite:
    """按 task_id 分派的脚本后端。

    ScriptedBackend 按 (revision, step) 索引，多个任务共用一个实例会串味；
    复合场景必须按任务分派。只用于确定性测试，真实模型走 --backend deepseek。
    """

    name = "scripted-composite"

    def __init__(self, *, collide: bool = False, token_cost: int = 400,
                 review_for=None) -> None:
        # collide=True 时 t2 会去写 t1 的文件，用来演示同层并行冲突
        self.collide = collide
        self.token_cost = token_cost
        # review_for(root_goal, specs) -> (sufficient, missing)。默认「看不出问题」——
        # 脚本后端不假装有语义判断力，语义复核的真实表现只能用真实模型测。
        self.review_for = review_for
        self.review_calls = 0
        self._cursor: dict[tuple[str, int], int] = {}

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        spec = ctx.task_spec
        key = (spec.id, spec.revision)
        i = self._cursor.get(key, 0)
        self._cursor[key] = i + 1

        target = spec.scope[0]
        if i == 0:
            return (
                ToolCall("write_file", {"path": target, "content": _SOLUTIONS[target]},
                         thought=f"写 {target}"),
                self.token_cost,
            )
        if i == 1 and self.collide and spec.id == "t2_format":
            # 同层的另一个任务的产出 —— 只有在 scope 被改宽时才写得进去
            return (
                ToolCall("write_file", {"path": "parse.py", "content": _SOLUTIONS["parse.py"]},
                         thought="顺手也改一下 parse.py"),
                self.token_cost,
            )
        return Finish(output={"file": target}, summary=f"{target} 写好了"), self.token_cost

    def triage(self, signals: list[Signal]) -> tuple[list[Triage], int]:
        return [Triage(s.id, "ignore", "scripted") for s in signals], 10

    def decide_interrupt(self, spec, signals, ctx, *, history=None) -> tuple[ArchitectVerdict, int]:
        return (
            ArchitectVerdict(
                action="MODIFY_TASK",
                rationale="脚本后端：放宽 scope 后重试",
                complexity_score=0.3,
                spec_changes={"scope": [*spec.scope, "parse.py"]},
            ),
            self.token_cost,
        )

    def summarize(self, ctx: AgentContext) -> tuple[str, int]:
        return "已产出：" + ", ".join(a.content_ref for a in ctx.produced), 50

    def verify(self, spec, ctx) -> tuple[bool, str, int]:
        return True, "脚本后端：非机器可检项默认通过", 10

    def probe(self, spec, ctx, excerpts) -> tuple[bool, str, int]:
        return True, "脚本后端：中间产出默认在轨", 10

    def review_decomposition(self, root_goal, specs) -> tuple[bool, list[str], int]:
        self.review_calls += 1
        if self.review_for is None:
            return True, [], 20
        sufficient, missing = self.review_for(root_goal, specs)
        return sufficient, list(missing), 20


ROOT_GOAL = (
    "做一个把 'name,42' 这样的文本行渲染成 'name = 42' 报告的小工具："
    "解析、格式化、组装成 render(lines) 接口，最后整体校验一遍。"
)


def build(
    workspace: str | None = None,
    *,
    store=None,
    backend=None,
    log=print,
    drop: str | None = None,
):
    """drop 传一个子任务 id 就把它从拆解里去掉 —— 用来验证拆解复核能不能发现遗漏。

    这不是「模拟一个错误」，而是**制造一个真实的拆解缺陷**：少掉 t2_format 之后，
    没有任何子任务的验收标准还覆盖「格式化」这一环，而原始目标里它是明写的。
    """
    ws = build_workspace(workspace)
    specs = [s for s in build_specs(ws) if s.id != drop]
    if drop:
        # 依赖也要跟着摘掉，否则拓扑排序会直接报「依赖不存在」，
        # 那样测的就是图检查而不是语义复核了
        specs = [s.bump(revision=1, depends_on=[d for d in s.depends_on if d != drop])
                 for s in specs]
    sched = Scheduler(
        specs,
        backend=backend or ScriptedComposite(),
        store=store or SqliteStore(),
        human_gate=AutoApproveGate(),
        root_goal=ROOT_GOAL,
        log=log,
    )
    return sched, ws
