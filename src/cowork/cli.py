"""CLI + 结构化日志（§10.2：界面层暂不做）。

    python -m cowork.cli demo             跑演示场景
    python -m cowork.cli demo --json      结构化日志（每行一条 JSON）
    python -m cowork.cli inspect <db>     导出某个库里的 DecisionRecord
    python -m cowork.cli bench            M2 参数实测跑批（§12 M2）
    python -m cowork.cli bench-report     从跑批记录出参数结论
    python -m cowork.cli bench-review     跨模型复核对照（§12 M7 7.2）
"""

from __future__ import annotations

import argparse
import json
import sys


# 每个供应商的端点、key 来源、默认模型分工。
# models = (subagent, architect, triage)。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        # v4 起 DeepSeek 只暴露 deepseek-v4-flash / deepseek-v4-pro 两个 id
        # （`GET /v1/models`），deepseek-chat / deepseek-reasoner 只剩别名。
        # 三个角色统一 flash：**这等于放弃了「架构师用推理档」这条分工**
        # （§4.1），要拿回来设 COWORK_ARCHITECT_MODEL=deepseek-v4-pro。
        # 注意 §11.6 / §11.9 / §11.11 的实测数据都出自 deepseek-reasoner，
        # 换档之后那些数字不能直接外推。
        "models": ("deepseek-v4-flash",) * 3,
    },
    "kimi": {
        # Moonshot 的 model id 改得比较勤，用 GET /v1/models 查当前账号可用的
        "base": "https://api.moonshot.cn/v1",
        "key_env": "MOONSHOT_API_KEY",
        "models": ("kimi-k3",) * 3,
    },
    "openai": {
        # 走 LiteLLM 或任意 OpenAI 兼容端点，全靠环境变量指定
        "base": "http://localhost:4000/v1",
        "key_env": "COWORK_LLM_API_KEY",
        "models": (None, "deepseek-chat", None),
    },
}


def _make_store(kind: str):
    if kind == "sqlite":
        from .store import SqliteStore

        return SqliteStore()
    if kind == "pg":
        from .store.postgres import PostgresStore

        return PostgresStore()
    raise ValueError(kind)


# 复核者的默认供应商。§11.11 实测：kimi-k3 的判别力 J 0.98，deepseek-reasoner
# 0.66，且后者在同一份输入上会翻面 —— 复核结论要驱动「重生成还是升级给人」，
# 裁决抖动等于把噪声接进控制流，所以默认选稳的那个。
DEFAULT_REVIEWER = "kimi"


def resolve_reviewer(backend: str, reviewer: str) -> str | None:
    """把 --reviewer 的 auto / none 解析成具体供应商。

    auto 的规则只有一条意图：**尽量让复核者和拆解者不是同一个模型**。
    - 脚本后端 → None：脚本后端没有语义判断力，为它花一次真实调用没有意义
    - 拆解者本来就是 kimi → 换 deepseek 复核，独立性比「用更强的那个」优先
    - 其余 → kimi
    """
    if reviewer == "none":
        return None
    if reviewer != "auto":
        return reviewer
    if backend == "scripted":
        return None
    return "deepseek" if backend == DEFAULT_REVIEWER else DEFAULT_REVIEWER


def _make_backend(kind: str):
    import os

    if kind == "scripted":
        return None  # demo.build / demo_composite.build 会用各自的脚本后端
    if kind == "anthropic":
        from .llm.anthropic_backend import AnthropicBackend

        return AnthropicBackend()
    if kind in PROVIDERS:
        from .llm.openai_compat import OpenAICompatBackend

        p = PROVIDERS[kind]
        sub, arch, triage = p["models"]
        # base_url / api_key 都显式解析：两家 key 同时存在时，
        # 靠后端内部的回退链会拿错 key（实测踩过）
        return OpenAICompatBackend(
            base_url=os.environ.get("COWORK_LLM_BASE_URL") or p["base"],
            api_key=os.environ.get("COWORK_LLM_API_KEY") or os.environ.get(p["key_env"]),
            subagent_model=os.environ.get("COWORK_SUBAGENT_MODEL", sub),
            architect_model=os.environ.get("COWORK_ARCHITECT_MODEL", arch),
            triage_model=os.environ.get("COWORK_TRIAGE_MODEL", triage),
        )
    raise ValueError(kind)


def _run_demo(args: argparse.Namespace) -> int:
    from .demo import build

    lines: list[str] = []
    log = lines.append if args.json else print
    orch, ws = build(
        args.workspace,
        store=_make_store(args.store),
        backend=_make_backend(args.backend),
        use_docker=args.docker,
    )
    orch.log = log

    print(f"workspace: {ws}", file=sys.stderr)
    result = orch.run()

    if args.json:
        for ln in lines:
            print(json.dumps({"log": ln}, ensure_ascii=False))
        print(
            json.dumps(
                {
                    "task_id": result.state.spec.id,
                    "status": result.state.status.value,
                    "revision": result.state.spec.revision,
                    "steps": result.state.current_step,
                    "interrupts": result.state.interrupt_count,
                    "tokens": result.state.tokens_used,
                    "output": result.output,
                    "decisions": [d.to_dict() for d in result.decisions],
                    "signals": [
                        s.to_dict() for s in orch.store.signals_for(result.state.spec.id)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print()
        print(f"最终状态   {result.state.status.value}")
        print(f"revision   {result.state.spec.revision}")
        print(f"总 step    {result.state.current_step}")
        print(f"中断次数   {result.state.interrupt_count}")
        print(f"token      {result.state.tokens_used}")
        print(f"产出       {result.output}")
        print()
        print("信号流水:")
        for s in orch.store.signals_for(result.state.spec.id):
            print(f"  {s.level.value} {s.type.value:<18} {s.source.value:<8} {s.disposition.value}")

    return 0 if result.state.status.value == "COMPLETED" else 1


def _inspect(args: argparse.Namespace) -> int:
    from .store import SqliteStore

    store = SqliteStore(args.db)
    for state in store.list_tasks():
        print(f"{state.spec.id}  {state.status.value}  rev={state.spec.revision}")
        for d in store.decisions_for(state.spec.id):
            print(f"  - {d.decider.value} {d.action.value} :: {d.rationale}")
    return 0


def _run_composite(args: argparse.Namespace) -> int:
    from .demo_composite import build

    lines: list[str] = []
    log = lines.append if args.json else print
    sched, ws = build(
        args.workspace, store=_make_store(args.store),
        backend=_make_backend(args.backend), log=log,
        # 复核者换一家供应商就是 M7 7.1 的全部内容
        reviewer_backend=_make_backend(reviewer) if (reviewer := resolve_reviewer(
            args.backend, args.reviewer)) else None,
    )
    print(f"workspace: {ws}", file=sys.stderr)
    result = sched.run(max_cycles=args.max_cycles)

    if args.json:
        for ln in lines:
            print(json.dumps({"log": ln}, ensure_ascii=False))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print()
        plan = result.plan
        print(f"分层     {[[t.id for t in x] for x in plan.layers]}")
        print(f"并行度   {plan.max_parallel}（可分解 {plan.decomposable}）")
        for issue in plan.issues:
            print(f"计划问题 {issue.kind}: {issue.detail}")
        print()
        for tid, r in result.results.items():
            print(f"  {tid:<12} {r.state.status.value:<14} rev={r.state.spec.revision} "
                  f"step={r.state.current_step} 中断={r.state.interrupt_count} "
                  f"token={r.state.tokens_used}")
        print(f"\n冲突     {len(result.conflicts)} 条")
        for a in result.arbitrations:
            print(f"  仲裁 {a['resource']} -> {a['action']}（{a['decider']}）")
        print(f"总耗时   {result.wall_seconds:.1f}s")
        print(f"整体     {'全部完成' if result.completed else '未全部完成'}")

    return 0 if result.completed else 1


def _bench(args: argparse.Namespace) -> int:
    import time
    from pathlib import Path

    from .bench.runner import default_tasks, run_batch

    if args.backend == "scripted":
        print("bench 需要真实模型：脚本后端上测出的参数是自证的假数据（§12 M2 排序理由）",
              file=sys.stderr)
        return 2

    tasks = default_tasks(args.tasks)
    total = len(tasks) * args.repeat
    print(f"任务 {len(tasks)} × {args.repeat} 次 = {total} 次运行，"
          f"后端 {args.backend}，并发 {args.workers}", file=sys.stderr)

    started = time.monotonic()

    def progress(rec, done: int, total_: int) -> None:
        elapsed = time.monotonic() - started
        eta = elapsed / done * (total_ - done)
        print(
            f"[{done}/{total_}] {rec.task_id}#{rec.run_index} {rec.status} "
            f"中断={rec.interrupts} token={rec.tokens} {rec.wall_seconds:.0f}s "
            f"ETA {eta / 60:.1f}min" + (f" ERROR {rec.error.splitlines()[-1][:80]}" if rec.error else ""),
            file=sys.stderr,
        )

    out = Path(args.out)
    run_batch(
        tasks,
        backend_factory=lambda: _make_backend(args.backend),
        repeat=args.repeat,
        out_path=out,
        workers=args.workers,
        progress=progress,
    )
    print(f"\n记录写入 {out}", file=sys.stderr)
    return _bench_report(argparse.Namespace(records=str(out), json=False))


def _plan(args: argparse.Namespace) -> int:
    """从一个自然语言目标拆出可派发的子任务（§12 M7 7.3 / 7.4）。"""
    import tempfile
    from pathlib import Path

    from .agent.architect import Architect, AutoApproveGate, CliGate, SpecTemplate
    from .policy import DEFAULT_POLICY
    from .types import SandboxProfile

    if args.backend == "scripted":
        print("拆解需要真实模型：脚本后端没有拆解能力", file=sys.stderr)
        return 2

    ws = Path(args.workspace or tempfile.mkdtemp(prefix="cowork-plan-"))
    ws.mkdir(parents=True, exist_ok=True)
    reviewer = resolve_reviewer(args.backend, args.reviewer)
    print(f"workspace: {ws}", file=sys.stderr)
    print(f"拆解者 {args.backend} / 复核者 {reviewer or '（同拆解者）'}", file=sys.stderr)

    architect = Architect(
        _make_backend(args.backend),
        _make_store(args.store),
        policy=DEFAULT_POLICY,
        # 非交互时用 AutoApproveGate：它不引入人的判断，只是把「有人配置了
        # 自动放行」显式化。想真的自己拍板就 --gate cli。
        human_gate=CliGate() if args.gate == "cli" else AutoApproveGate(),
        reviewer_backend=_make_backend(reviewer) if reviewer else None,
    )
    result = architect.plan(
        args.goal,
        SpecTemplate(sandbox=SandboxProfile(workspace=str(ws), allowed_binaries=("python",))),
        log=(lambda _m: None) if args.json else print,
    )

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.accepted else 1

    print()
    print(f"终局       {result.status}（{result.decider}）")
    if args.run and not result.accepted:
        print("拆解未通过，不派发 —— 拆错了就不该开跑（同 Scheduler 的复核前置）",
              file=sys.stderr)
    print(f"生成轮次   {result.attempts}   token {result.tokens}")
    if result.escalation_reason:
        print(f"升级原因   {result.escalation_reason}")
    print(f"理由       {result.rationale}")
    print()
    for s in result.specs:
        deps = f"  ← {', '.join(s.depends_on)}" if s.depends_on else ""
        print(f"  {s.id}  [{s.task_class.value}]  scope={s.scope}{deps}")
        print(f"    {s.goal}")
        for c in s.acceptance:
            print(f"      [{'✓cmd' if c.machine_checkable else ' 人判'}] {c.description}")
    if result.review:
        for i in result.review.structural:
            print(f"\n结构问题   {i.kind}: {i.detail}")
        for m in result.review.missing:
            print(f"复核缺口   {m}")

    if not (args.run and result.accepted):
        return 0 if result.accepted else 1

    # 从自然语言目标一路跑到产出：拆解 → 分层 → 并行执行（§12 M7 出口 1）。
    # 复核已经在 plan() 里做过了，所以这里 Scheduler 不再给 root_goal ——
    # 否则同一份拆解会被复核两次，白花一次调用。
    from .scheduler import Scheduler

    print("\n" + "=" * 60)
    sched = Scheduler(
        result.specs,
        backend=architect.backend,
        store=architect.store,
        human_gate=architect.human_gate,
        log=print,
    )
    outcome = sched.run(max_cycles=args.max_cycles)
    print()
    for tid, r in outcome.results.items():
        print(f"  {tid:<18} {r.state.status.value:<14} step={r.state.current_step} "
              f"中断={r.state.interrupt_count} token={r.state.tokens_used}")
    print(f"\n整体       {'全部完成' if outcome.completed else '未全部完成'}"
          f"   耗时 {outcome.wall_seconds:.1f}s")
    return 0 if outcome.completed else 1


def _bench_review(args: argparse.Namespace) -> int:
    import time
    from pathlib import Path

    from .bench.review_ab import run_batch, select_cases

    if args.backend == "scripted":
        print("跨模型复核对照需要真实模型：脚本后端没有语义判断力，测出来的是自证的假数据",
              file=sys.stderr)
        return 2

    cases = select_cases(args.cases)
    if not cases:
        print(f"--cases {args.cases!r} 没匹配到任何用例", file=sys.stderr)
        return 2

    # arm 名就是供应商名。等于 --backend 的那个 arm 走 reviewer_backend=None，
    # 也就是 M5b 的同模型基线 —— 它是这次对照的对照组，别把它去掉。
    arms: dict[str, object] = {}
    for name in [x.strip() for x in args.arms.split(",") if x.strip()]:
        arms[name] = None if name == args.backend else (lambda n=name: _make_backend(n))

    total = len(cases) * len(arms) * args.repeat
    print(f"用例 {len(cases)} × arm {len(arms)} × {args.repeat} 次 = {total} 次复核调用，"
          f"基准后端 {args.backend}，arm={list(arms)}", file=sys.stderr)

    started = time.monotonic()

    def progress(rec, done: int, total_: int) -> None:
        elapsed = time.monotonic() - started
        eta = elapsed / done * (total_ - done)
        verdict = "ERROR" if rec.error else ("充分" if rec.sufficient else "报缺口")
        want = "完整" if rec.complete else f"缺陷={rec.defect}"
        print(f"[{done}/{total_}] {rec.case_id}({want}) arm={rec.arm} -> {verdict} "
              f"token={rec.tokens} {rec.wall_seconds:.1f}s ETA {eta / 60:.1f}min",
              file=sys.stderr)

    out = Path(args.out)
    run_batch(
        cases,
        base_factory=lambda: _make_backend(args.backend),
        arms=arms,
        repeat=args.repeat,
        out_path=out,
        workers=args.workers,
        progress=progress,
    )
    print(f"\n记录写入 {out}", file=sys.stderr)
    return _bench_review_report(argparse.Namespace(records=str(out), json=False))


def _bench_review_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .bench.review_ab import load, render, summarize

    summary = summarize(load(Path(args.records)))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0


def _bench_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .bench.analyze import load, render, summarize

    summary = summarize(load(Path(args.records)))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0


def main(argv: list[str] | None = None) -> int:
    from .config import find_env_file, load_env

    # 密钥从 .env 读，不走命令行参数 —— 命令行会进 shell history 和进程列表。
    # 环境变量优先于文件，所以 CI / 容器里覆盖不受影响。
    env_file = find_env_file()
    applied = load_env(env_file)
    if applied:
        # 只打键名，不打值
        print(f"[env ] 从 {env_file} 载入: {', '.join(applied)}", file=sys.stderr)

    p = argparse.ArgumentParser(prog="cowork")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="跑 L0 -> 中断 -> REBASE -> 恢复 演示")
    d.add_argument("--workspace", default=None)
    d.add_argument("--json", action="store_true")
    d.add_argument("--store", choices=["sqlite", "pg"], default="sqlite")
    d.add_argument(
        "--backend",
        choices=["scripted", "anthropic", "deepseek", "kimi", "openai"],
        default="scripted",
        help=(
            "anthropic 需 ANTHROPIC_API_KEY；deepseek 需 DEEPSEEK_API_KEY；"
            "kimi 需 MOONSHOT_API_KEY；openai 走 COWORK_LLM_BASE_URL（默认 LiteLLM）"
        ),
    )
    d.add_argument("--docker", action="store_true", help="用 Docker 沙箱跑工具调用")
    d.set_defaults(func=_run_demo)

    i = sub.add_parser("inspect", help="导出某个 SQLite 库里的任务与决策")
    i.add_argument("db")
    i.set_defaults(func=_inspect)

    c = sub.add_parser("composite", help="复合任务演示：并行 + 冲突检测（§12 M4）")
    c.add_argument("--workspace", default=None)
    c.add_argument("--json", action="store_true")
    c.add_argument("--store", choices=["sqlite", "pg"], default="sqlite")
    c.add_argument("--backend", choices=["scripted", "anthropic", "deepseek", "kimi", "openai"],
                   default="scripted")
    c.add_argument("--reviewer",
                   choices=["auto", "none", "anthropic", "deepseek", "kimi", "openai"],
                   default="auto",
                   help=f"拆解复核的供应商（§12 M7 7.1）。auto：真实后端时用 "
                        f"{DEFAULT_REVIEWER}、脚本后端时不复核；none 退回同模型复核")
    c.add_argument("--max-cycles", type=int, default=4, dest="max_cycles")
    c.set_defaults(func=_run_composite)

    b = sub.add_parser("bench", help="M2 参数实测跑批（§12 M2）")
    b.add_argument("--backend", choices=["anthropic", "deepseek", "kimi", "openai"],
                   default="deepseek")
    b.add_argument("--repeat", type=int, default=5,
                   help="每个任务跑几次。§11.5d：单次运行是噪声，不要低于 5")
    b.add_argument("--workers", type=int, default=4)
    b.add_argument("--tasks", default=None, help="逗号分隔的任务 id 或类别，默认全跑")
    b.add_argument("--out", default="bench_runs.jsonl")
    b.set_defaults(func=_bench)

    br = sub.add_parser("bench-report", help="从跑批记录出参数结论")
    br.add_argument("records")
    br.add_argument("--json", action="store_true")
    br.set_defaults(func=_bench_report)

    pl = sub.add_parser("plan", help="从一个自然语言目标拆出子任务（§12 M7 7.3/7.4）")
    pl.add_argument("goal", help="原始目标，一句自然语言")
    pl.add_argument("--backend", choices=["anthropic", "deepseek", "kimi", "openai"],
                    default="deepseek", help="拆解者")
    pl.add_argument("--reviewer",
                    choices=["auto", "none", "anthropic", "deepseek", "kimi", "openai"],
                    default="auto", help=f"复核者，auto = {DEFAULT_REVIEWER}")
    pl.add_argument("--gate", choices=["auto", "cli"], default="auto",
                    help="升级给人时谁来答：auto 自动放行，cli 你自己在终端拍板")
    pl.add_argument("--workspace", default=None)
    pl.add_argument("--store", choices=["sqlite", "pg"], default="sqlite")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--run", action="store_true",
                    help="拆解通过后直接派发执行 —— 从自然语言目标一路跑到产出。花真钱")
    pl.add_argument("--max-cycles", type=int, default=4, dest="max_cycles")
    pl.set_defaults(func=_plan)

    rv = sub.add_parser("bench-review", help="跨模型复核对照实测（§12 M7 7.2）")
    rv.add_argument("--backend", choices=["anthropic", "deepseek", "kimi", "openai"],
                    default="deepseek", help="拆解者/同模型基线用的供应商")
    rv.add_argument("--arms", default="deepseek,kimi",
                    help="逗号分隔的复核者供应商。等于 --backend 的那个是同模型基线")
    rv.add_argument("--repeat", type=int, default=5,
                    help="每个用例每个 arm 跑几次。低于 5 的结果是噪声（§11.5d）")
    rv.add_argument("--workers", type=int, default=4)
    rv.add_argument("--cases", default=None,
                    help="逗号分隔的用例 id / 家族名 / 缺陷形态，默认全跑")
    rv.add_argument("--out", default="review_ab.jsonl")
    rv.set_defaults(func=_bench_review)

    rvr = sub.add_parser("bench-review-report", help="只出跨模型复核报告，不重跑")
    rvr.add_argument("records")
    rvr.add_argument("--json", action="store_true")
    rvr.set_defaults(func=_bench_review_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
