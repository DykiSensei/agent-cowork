"""跑批 + 仪表化。

**只包装，不改被测对象**。所有观测都通过代理 Backend 和 Store 拿到：
Runtime、Orchestrator、Architect 一行都不动。理由在包的 __init__ 里 ——
被实测工具改过的系统，测出来的参数不能用。

每条记录写一行 JSON（JSONL）。字段设计对着 §12 M2 那张表：

    step_seconds        -> 中断响应延迟（= 一个 step 的时长，抢占只在 step 边界发生）
    checkpoint_seconds  -> checkpoint 写入开销占比（证伪了风险 #1 的前提）
    decisions[].score   -> complexity_threshold（ROC）
    rebase_count/intent -> max_rebase（连续 REBASE 后的偏离度）
    architect_calls     -> 分诊频次 × 单次成本（软信号值不值得做批处理）
    interrupts/status   -> max_interrupts（中断次数 vs 成功率）
    task_trace          -> budget_escalation_ratio（触发点距实际超支的距离）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..agent.architect import AutoApproveGate
from ..orchestrator import Orchestrator
from ..policy import DEFAULT_POLICY, Policy
from ..store import SqliteStore
from .tasks import ALL_TASKS, BENCH_TASKS, BenchTask

# 一次中断决策最多跑几轮。默认 8 在 ESCALATE 类任务上会白烧 token
# （矛盾的验收标准永远改不好），实测用 5 —— 足够看到 max_interrupts 生效。
BENCH_MAX_CYCLES = 5


# --------------------------------------------------------------------------- #
# 仪表化代理
# --------------------------------------------------------------------------- #


class _TimedBackend:
    """给每次模型调用打上墙钟耗时与 token。

    next_step 的耗时就是**中断响应延迟的上界** —— 抢占只在 step 边界发生，
    所以外部中断最坏要等当前 step 跑完（§5.1）。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "?")
        self.step_seconds: list[float] = []
        self.calls: list[dict[str, Any]] = []

    def _record(self, kind: str, t0: float, tokens: int) -> None:
        self.calls.append({"kind": kind, "seconds": time.monotonic() - t0, "tokens": tokens})

    def next_step(self, ctx):
        t0 = time.monotonic()
        action, tokens = self._inner.next_step(ctx)
        dt = time.monotonic() - t0
        self.step_seconds.append(dt)
        self.calls.append({"kind": "next_step", "seconds": dt, "tokens": tokens})
        return action, tokens

    def triage(self, signals):
        t0 = time.monotonic()
        out, tokens = self._inner.triage(signals)
        self.calls.append(
            {"kind": "triage", "seconds": time.monotonic() - t0,
             "tokens": tokens, "batch": len(signals)}
        )
        return out, tokens

    def decide_interrupt(self, spec, signals, ctx, *, history=None):
        t0 = time.monotonic()
        verdict, tokens = self._inner.decide_interrupt(spec, signals, ctx, history=history)
        self._record("decide_interrupt", t0, tokens)
        return verdict, tokens

    def summarize(self, ctx):
        t0 = time.monotonic()
        out, tokens = self._inner.summarize(ctx)
        self._record("summarize", t0, tokens)
        return out, tokens

    def verify(self, spec, ctx):
        t0 = time.monotonic()
        passed, reason, tokens = self._inner.verify(spec, ctx)
        self._record("verify", t0, tokens)
        return passed, reason, tokens

    def probe(self, spec, ctx, excerpts):
        t0 = time.monotonic()
        on_track, reason, tokens = self._inner.probe(spec, ctx, excerpts)
        self.calls.append(
            {"kind": "probe", "seconds": time.monotonic() - t0, "tokens": tokens,
             "on_track": on_track, "excerpt_chars": sum(len(v) for v in excerpts.values())}
        )
        return on_track, reason, tokens


class _TracingStore:
    """记录 checkpoint 写入耗时，以及每次 save_task 时的状态快照。

    save_task 在每个状态转换点都会被调用（含 status=INTERRUPTED 之后），
    所以这串快照就是 token 消耗轨迹 —— budget_escalation_ratio 要的
    「触发点距实际超支的距离」只能从轨迹里算，终值算不出来。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.checkpoint_seconds: list[float] = []
        self.task_trace: list[dict[str, Any]] = []
        self._t0 = time.monotonic()

    def save_checkpoint(self, cp):
        t0 = time.monotonic()
        out = self._inner.save_checkpoint(cp)
        self.checkpoint_seconds.append(time.monotonic() - t0)
        return out

    def save_task(self, state):
        self.task_trace.append(
            {
                "at": round(time.monotonic() - self._t0, 4),
                "status": state.status.value,
                "step": state.current_step,
                "interrupts": state.interrupt_count,
                "tokens": state.tokens_used,
            }
        )
        return self._inner.save_task(state)

    def __getattr__(self, item):
        return getattr(self._inner, item)


# 与 escalation.should_escalate 的措辞耦合：LLM 自评那条路径的理由以此开头。
# test_bench.py 里有一条断言钉住它，改了措辞测试会红。
_COMPLEXITY_PREFIX = "LLM 自评"


def _escalation_kind(reason: str | None) -> str:
    if not reason:
        return "none"
    return "complexity" if reason.startswith(_COMPLEXITY_PREFIX) else "deterministic"


# --------------------------------------------------------------------------- #
# 单次运行
# --------------------------------------------------------------------------- #


@dataclass
class RunRecord:
    task_id: str
    category: str
    run_index: int
    backend: str
    status: str = "ERROR"
    revision: int = 0
    steps: int = 0
    interrupts: int = 0
    tokens: int = 0
    wall_seconds: float = 0.0
    rebase_count: int = 0
    completed: bool = False
    intent_ok: bool | None = None
    intent_detail: str = ""
    step_seconds: list[float] = field(default_factory=list)
    checkpoint_seconds: list[float] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    task_trace: list[dict] = field(default_factory=list)
    token_budget: int = 0
    should_escalate: bool = False
    probe_count: int = 0
    probe_tokens: int = 0
    silence_policy: str = "TRUST"
    probe_interval_s: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def run_once(
    task: BenchTask,
    *,
    backend_factory: Callable[[], Any],
    run_index: int,
    root: Path,
    policy: Policy = DEFAULT_POLICY,
    max_cycles: int = BENCH_MAX_CYCLES,
) -> RunRecord:
    rec = RunRecord(
        task_id=task.id,
        category=task.category.value,
        run_index=run_index,
        backend="?",
        should_escalate=task.should_escalate,
    )
    ws = root / f"{task.id}-{run_index}"
    t0 = time.monotonic()
    try:
        task.materialize(ws)
        spec = task.spec(ws)
        rec.token_budget = spec.token_budget
        rec.silence_policy = spec.silence_policy.value
        rec.probe_interval_s = spec.probe_interval_s

        backend = _TimedBackend(backend_factory())
        rec.backend = backend.name
        store = _TracingStore(SqliteStore())

        orch = Orchestrator(
            spec,
            backend=backend,
            store=store,
            policy=policy,
            # 升级必须能被记录下来又不能卡住跑批：AutoApproveGate 采纳 LLM 裁决，
            # 但 escalation_reason 仍然如实落进 DecisionRecord。
            human_gate=AutoApproveGate(),
            log=lambda _msg: None,
        )
        result = orch.run(max_cycles=max_cycles)

        rec.status = result.state.status.value
        rec.completed = rec.status == "COMPLETED"
        rec.revision = result.state.spec.revision
        rec.steps = result.state.current_step
        rec.interrupts = result.state.interrupt_count
        rec.tokens = result.state.tokens_used
        rec.decisions = [
            {
                "action": d.action.value,
                "decider": d.decider.value,
                "score": d.complexity_score,
                "escalation_reason": d.escalation_reason,
                "escalation_kind": _escalation_kind(d.escalation_reason),
                "resume_mode": d.resume_mode.value if d.resume_mode else None,
            }
            for d in result.decisions
        ]
        rec.rebase_count = sum(1 for d in rec.decisions if d["resume_mode"] == "REBASE")
        rec.signals = [
            {"type": s.type.value, "level": s.level.value,
             "source": s.source.value, "disposition": s.disposition.value}
            for s in store.signals_for(spec.id)
        ]
        rec.step_seconds = [round(x, 4) for x in backend.step_seconds]
        rec.checkpoint_seconds = [round(x, 6) for x in store.checkpoint_seconds]
        rec.calls = [
            {**c, "seconds": round(c["seconds"], 4)} for c in backend.calls
        ]
        rec.task_trace = store.task_trace
        rec.probe_count = orch.probe_count
        rec.probe_tokens = orch.probe_tokens
        rec.intent_ok, rec.intent_detail = _intent_check(task, ws)
    except Exception:
        rec.error = traceback.format_exc()[-2000:]
    rec.wall_seconds = round(time.monotonic() - t0, 3)
    return rec


def _intent_check(task: BenchTask, ws: Path) -> tuple[bool | None, str]:
    """终局产出是否仍满足**原始 goal** 的语义。

    脚本写在 workspace 之外并以绝对路径执行 —— 它绝不能出现在 workspace 里，
    否则 Subagent 能读到它，等于把答案发下去。这是 max_rebase 偏离度的唯一量具：
    验收标准会被架构师改，原始意图不会。
    """
    if not task.intent_check:
        return None, "该任务无独立意图检查"
    if not (ws / "solution.py").exists():
        return False, "没有产出 solution.py"
    tmp = Path(tempfile.mkdtemp(prefix="intent-")) / "intent_check.py"
    tmp.write_text(task.intent_check, encoding="utf-8")
    # 脚本在 workspace 外，所以 sys.path[0] 是它自己的目录、不是 cwd ——
    # 得显式把 workspace 加进 PYTHONPATH，否则 import solution 必然失败。
    env = {**os.environ, "PYTHONPATH": str(ws)}
    proc = subprocess.run(
        [sys.executable, str(tmp)],
        cwd=str(ws), env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-400:]


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #


def run_batch(
    tasks: Iterable[BenchTask],
    *,
    backend_factory: Callable[[], Any],
    repeat: int,
    out_path: Path,
    root: Path | None = None,
    workers: int = 4,
    policy: Policy = DEFAULT_POLICY,
    progress: Callable[[RunRecord, int, int], None] | None = None,
) -> list[RunRecord]:
    tasks = list(tasks)
    root = root or Path(tempfile.mkdtemp(prefix="cowork-bench-"))
    jobs = [(t, i) for t in tasks for i in range(1, repeat + 1)]
    total = len(jobs)
    records: list[RunRecord] = []

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_once,
                    t,
                    backend_factory=backend_factory,
                    run_index=i,
                    root=root,
                    policy=policy,
                ): (t, i)
                for t, i in jobs
            }
            for done, fut in enumerate(as_completed(futures), start=1):
                rec = fut.result()
                records.append(rec)
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()  # 跑批很长，中途要能看进度，也要能中断后不丢数据
                if progress:
                    progress(rec, done, total)
    return records


def default_tasks(only: str | None = None) -> list[BenchTask]:
    """默认只跑 M2 任务集 —— M3 的对照任务要显式点名。

    M2 的结论要能被原样复现，所以默认集合不能因为后续里程碑加任务而变化。
    """
    if not only:
        return list(BENCH_TASKS)
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    return [t for t in ALL_TASKS if t.id in wanted or t.category.value in wanted]
