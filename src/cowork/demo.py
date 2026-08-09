"""演示场景：一个 CODE 类任务走完 TEST_FAILED -> 中断 -> REBASE -> 恢复。

**场景设计的要害**：验收标准里藏着一条 goal 没写的项目约定——「空串不算回文」。
这条任何模型都推断不出来，只能靠失败信号发现。这不是刁难，正是 MAST 里
「规格不清 42%」的典型形态：规格的缺失部分只在验收时才暴露。

早期版本把「需要归一化大小写与标点」当作隐藏项，M1.3 实测发现真实模型
（DeepSeek）会直接写对，三次运行零中断——场景对真实模型失去了区分度。
换成不可推断的约定后，脚本后端和真实模型走的是同一条链路。

故事线：
  rev1  Subagent 写出合理但不符合项目约定的实现 -> verify.py 失败 -> TEST_FAILED
  中断  架构师读到具体证据，判断是规格没说清 -> MODIFY_TASK，补一条验收标准
        （顶层任务 + MODIFY_TASK 命中 §7.2 确定性下限 -> 升级给人）
  REBASE goal 未变、只改 acceptance -> 走 §6.2 的第二条规则
        produced 保留、reasoning_trace 清空 -> 起新 Subagent
  rev2  新 Subagent 拿到补充后的标准，写出正确实现 -> 验收通过 -> COMPLETED
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .actions import Finish, ToolCall
from .agent.architect import AutoApproveGate
from .llm import ArchitectVerdict
from .llm.scripted import ScriptedBackend
from .orchestrator import Orchestrator
from .store import SqliteStore
from .types import Criterion, SandboxProfile, TaskClass, TaskSpec

VERIFY_PY = '''\
import sys
from solution import is_palindrome

CASES = [
    ("racecar", True),
    ("hello", False),
    ("A man, a plan, a canal: Panama", True),
    ("No 'x' in Nixon", True),
    # 本项目约定：空串不算回文。这条**故意不写在 goal 里** ——
    # 它是「规格不清」的载体，任何模型都推断不出来，只能靠失败信号发现。
    ("", False),
]

failures = [(s, e) for s, e in CASES if is_palindrome(s) is not e]
if failures:
    for s, e in failures:
        print(f"FAIL: is_palindrome({s!r}) -> {is_palindrome(s)}, expected {e}",
              file=sys.stderr)
    sys.exit(1)
print(f"OK: {len(CASES)} cases passed")
'''

NAIVE_SOLUTION = '''\
def is_palindrome(s: str) -> bool:
    return s == s[::-1]
'''

CORRECT_SOLUTION = '''\
def is_palindrome(s: str) -> bool:
    normalized = [c.lower() for c in s if c.isalnum()]
    if not normalized:
        return False
    return normalized == normalized[::-1]
'''

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"file": {"type": "string"}, "function": {"type": "string"}},
    "required": ["file", "function"],
}


def build_workspace(root: str | None = None) -> Path:
    ws = Path(root or tempfile.mkdtemp(prefix="cowork-demo-"))
    ws.mkdir(parents=True, exist_ok=True)
    # verify.py 由架构师放进 workspace，且**不在** scope 内 ——
    # Subagent 若尝试改它来让测试通过，会撞上 SCOPE_VIOLATION。
    (ws / "verify.py").write_text(VERIFY_PY, encoding="utf-8")
    return ws


def build_spec(workspace: Path) -> TaskSpec:
    return TaskSpec(
        goal="在 solution.py 里实现 is_palindrome(s) -> bool，判断字符串是否回文。",
        acceptance=[
            Criterion(
                id="c1",
                description="verify.py 全部用例通过",
                command=["python", "verify.py"],
            ),
        ],
        task_class=TaskClass.CODE,
        output_schema=OUTPUT_SCHEMA,
        sandbox=SandboxProfile(workspace=str(workspace), allowed_binaries=("python",)),
        scope=["solution.py"],
        tools=["write_file", "read_file", "list_files", "run"],
        max_steps=6,
        deadline_s=120.0,
        token_budget=50_000,
    )


def build_backend() -> ScriptedBackend:
    steps = {
        # rev 1：朴素实现，没做归一化
        (1, 0): ToolCall(
            "write_file",
            {"path": "solution.py", "content": NAIVE_SOLUTION},
            thought="先写最直接的实现",
        ),
        (1, 1): Finish(
            output={"file": "solution.py", "function": "is_palindrome"},
            summary="实现了 is_palindrome，直接比较反转字符串",
        ),
        # rev 2：拿到补充后的验收标准，加上归一化
        (2, 0): ToolCall(
            "write_file",
            {"path": "solution.py", "content": CORRECT_SOLUTION},
            thought="新增的验收标准要求忽略大小写与非字母数字字符",
        ),
        (2, 1): Finish(
            output={"file": "solution.py", "function": "is_palindrome"},
            summary="实现了归一化后的 is_palindrome",
        ),
    }

    def verdict_for(spec, signals) -> ArchitectVerdict:
        return ArchitectVerdict(
            action="MODIFY_TASK",
            rationale=(
                "verify.py 在空串用例上失败，说明本项目有一条 goal 没写明的约定。"
                "这是规格问题不是实现问题：补一条验收标准，goal 不变。"
            ),
            complexity_score=0.25,
            spec_changes={
                "added_criteria": [
                    {
                        "id": "c2",
                        "description": "空串不算回文，必须返回 False",
                    }
                ]
            },
        )

    return ScriptedBackend(steps, verdict_for=verdict_for)


def build(
    workspace: str | None = None,
    *,
    store=None,
    backend=None,
    use_docker: bool = False,
):
    """返回 (orchestrator, workspace)。

    store   None -> SqliteStore（默认）；传 PostgresStore 即换正式存储
    backend None -> ScriptedBackend（确定性）；传 AnthropicBackend 即接真实模型
    """
    ws = build_workspace(workspace)
    spec = build_spec(ws)
    if use_docker:
        spec = spec.bump(
            revision=1,
            sandbox=SandboxProfile(
                workspace=str(ws), allowed_binaries=("python",), use_docker=True
            ),
        )
    store = store or SqliteStore()
    orch = Orchestrator(
        spec,
        backend=backend or build_backend(),
        store=store,
        # 顶层任务 + MODIFY_TASK 会命中 §7.2 的确定性下限并升级给人。
        # 非交互演示用 AutoApproveGate 放行——它不引入人的判断，只是把
        #「有人配置了自动放行」显式化。真跑起来请换 CliGate。
        human_gate=AutoApproveGate(),
    )
    return orch, ws
