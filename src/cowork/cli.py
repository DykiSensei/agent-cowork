"""CLI + 结构化日志（§10.2：界面层暂不做）。

    python -m cowork.cli demo             跑演示场景
    python -m cowork.cli demo --json      结构化日志（每行一条 JSON）
    python -m cowork.cli inspect <db>     导出某个库里的 DecisionRecord
"""

from __future__ import annotations

import argparse
import json
import sys


def _make_store(kind: str):
    if kind == "sqlite":
        from .store import SqliteStore

        return SqliteStore()
    if kind == "pg":
        from .store.postgres import PostgresStore

        return PostgresStore()
    raise ValueError(kind)


def _make_backend(kind: str):
    import os

    if kind == "scripted":
        return None  # demo.build 会用默认的 ScriptedBackend
    if kind == "anthropic":
        from .llm.anthropic_backend import AnthropicBackend

        return AnthropicBackend()
    if kind in ("deepseek", "kimi", "openai"):
        from .llm.openai_compat import PRESETS, OpenAICompatBackend

        defaults = {
            # 架构师用推理型，Subagent / 分诊用便宜的对话型
            "deepseek": ("deepseek-chat", "deepseek-reasoner", "deepseek-chat"),
            "kimi": ("kimi-k2-0711-preview",) * 3,
            "openai": (None, "deepseek-chat", None),
        }[kind]
        base = os.environ.get("COWORK_LLM_BASE_URL")
        if not base and kind in PRESETS:
            base = PRESETS[kind if kind != "kimi" else "moonshot"]
        return OpenAICompatBackend(
            base_url=base,
            subagent_model=os.environ.get("COWORK_SUBAGENT_MODEL", defaults[0]),
            architect_model=os.environ.get("COWORK_ARCHITECT_MODEL", defaults[1]),
            triage_model=os.environ.get("COWORK_TRIAGE_MODEL", defaults[2]),
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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
