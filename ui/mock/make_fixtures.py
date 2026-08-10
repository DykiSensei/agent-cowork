"""重新生成 ui/fixtures/ —— 全部来自真实运行，一行手写 mock 都没有（M6 §10.5）。

手写的 mock 会和真实形状慢慢分叉，所以这里的做法是：用脚本后端（确定性、
不花钱、不需要 key）把一串真实场景跑进同一个 sqlite，再用 `cowork.views`
导出成 JSON —— 和将来 FastAPI 服务层吐的是同一份形状。

用法（仓库根目录）：

    PYTHONPATH=src python ui/mock/make_fixtures.py

产出：
    ui/fixtures/threads.json          线程列表（views.thread_list）
    ui/fixtures/<task_id>.json        每个线程的详情（views.task_detail）
    ui/fixtures/providers.json        供应商预设表（cli.PROVIDERS，设置页用）
中间库 ui/mock/fixtures.sqlite 是临时产物，删了重跑就是。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))  # 免 pip install 也能跑

from cowork import demo, demo_composite, views  # noqa: E402
from cowork.agent.architect import AutoApproveGate, HumanRuling  # noqa: E402
from cowork.cli import PROVIDERS  # noqa: E402
from cowork.orchestrator import Orchestrator  # noqa: E402
from cowork.store import SqliteStore  # noqa: E402
from cowork.types import Action, TaskEvent, TaskState, TaskStatus  # noqa: E402

DB = ROOT / "ui" / "mock" / "fixtures.sqlite"
OUT = ROOT / "ui" / "fixtures"

QUIET = lambda _: None  # 场景日志静默；views 导出才是产物


def run_demo(store, *, gate, max_cycles: int = 8, goal: str | None = None) -> TaskState:
    """跑一遍 demo 场景（TEST_FAILED → 裁决 → …），gate 决定终局。"""
    ws = demo.build_workspace(None)
    spec = demo.build_spec(ws)
    if goal:
        spec = replace(spec, goal=goal)
    orch = Orchestrator(
        spec, backend=demo.build_backend(), store=store, human_gate=gate, log=QUIET
    )
    return orch.run(max_cycles=max_cycles).state


class AbandonGate:
    """演示用：升级上来之后，人决定放弃。"""

    def review(self, spec, signals, verdict, reason):
        return HumanRuling(
            action=Action.ABANDON,
            rationale="人看过现场后决定放弃：收益抵不上继续折腾。",
            spec_changes={},
        )


def parked_task(store, goal: str, status: TaskStatus, logs: list[str]) -> str:
    """只存在于存储层的任务现场：RUNNING / INTERRUPTED 是进程快照，PENDING 是排队。

    它们不会出现在一次正常 run 的落库结果里（run() 返回时状态已经走过这些点），
    但服务进程重启后列表里就该是这样 —— 直接按现场存。
    """
    ws = demo.build_workspace(None)
    spec = replace(demo.build_spec(ws), goal=goal)
    state = TaskState(spec=spec, status=status)
    state.started_at = time.time()
    store.save_task(state)
    for text in logs:
        store.append_event(TaskEvent(task_id=spec.id, kind="log", text=text))
    if status is TaskStatus.INTERRUPTED:
        store.append_event(
            TaskEvent(task_id=spec.id, kind="status", payload={"status": status.value})
        )
    return spec.id


def main() -> None:
    DB.unlink(missing_ok=True)
    store = SqliteStore(str(DB))

    # 1. 一次完整的中断-改任务-恢复 → COMPLETED（AutoApproveGate 放行升级）
    done = run_demo(store, gate=AutoApproveGate())
    print("COMPLETED  ", done.spec.id)

    # 2. 没有人的入口 → 升级落 AWAITING_HUMAN（带真实的 suggestion）
    waiting = run_demo(store, gate=None)
    print("AWAITING   ", waiting.spec.id, waiting.status.value)
    assert waiting.status is TaskStatus.AWAITING_HUMAN

    # 3. 一轮就跑满 → FAILED
    failed = run_demo(store, gate=AutoApproveGate(), max_cycles=1)
    print("FAILED     ", failed.spec.id, failed.status.value)
    assert failed.status is TaskStatus.FAILED

    # 4. 人决定放弃 → ABANDONED
    abandoned = run_demo(store, gate=AbandonGate())
    print("ABANDONED  ", abandoned.spec.id, abandoned.status.value)
    assert abandoned.status is TaskStatus.ABANDONED

    # 5-7. 存储层现场：PENDING / RUNNING / INTERRUPTED
    parked_task(store, "整理 README 与目录结构", TaskStatus.PENDING, [])
    parked_task(
        store,
        "实现 word_count(text) 词频统计",
        TaskStatus.RUNNING,
        ["[RUN ] cycle=1 rev=1 agent=agent_0d1f2a3b4c step=0"],
    )
    parked_task(
        store,
        "生成周报草稿",
        TaskStatus.INTERRUPTED,
        [
            "[RUN ] cycle=1 rev=1 agent=agent_e55d09a1b2 step=0",
            "[STOP] VALIDATION_FAILED @step=2 interrupt_count=1",
        ],
    )

    # 8. 复合任务 → 父线程（合成）+ 4 个子任务
    sched, _ = demo_composite.build(store=store, log=QUIET)
    outcome = sched.run()
    print("COMPOSITE  ", sched.root_id, "completed =", outcome.completed)

    # ---- 导出：views 的真实输出，一行不改 ----
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("task_*.json"):
        old.unlink()
    threads = views.thread_list(store)
    (OUT / "threads.json").write_text(
        json.dumps(threads, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for t in threads:
        detail = views.task_detail(store, t["task_id"])
        (OUT / f"{t['task_id']}.json").write_text(
            json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("detail     ", t["task_id"], t["status"], f"events={len(detail['events'])}")

    # 供应商预设表（设置页的数据源；key 永远不走这里，只有 env 变量名）
    providers = [
        {
            "name": name,
            "base": p["base"],
            "key_env": p["key_env"],
            "models": {
                "subagent": p["models"][0],
                "architect": p["models"][1],
                "triage": p["models"][2],
            },
            "verified": p["verified"],
            "effort": p.get("effort"),
            "cache": p.get("cache", "unknown"),
        }
        for name, p in sorted(PROVIDERS.items())
    ]
    (OUT / "providers.json").write_text(
        json.dumps(providers, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n导出 {len(threads)} 条线程 + providers.json → {OUT}")


if __name__ == "__main__":
    main()
