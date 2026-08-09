"""CLI + 结构化日志（§10.2：界面层暂不做）。

    python -m cowork.cli demo             跑演示场景
    python -m cowork.cli demo --json      结构化日志（每行一条 JSON）
    python -m cowork.cli inspect <db>     导出某个库里的 DecisionRecord
"""

from __future__ import annotations

import argparse
import json
import sys


# 每个供应商的端点、key 来源、默认模型分工。
# models = (subagent, architect, triage)；架构师用推理型，其余用便宜的对话型。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "models": ("deepseek-chat", "deepseek-reasoner", "deepseek-chat"),
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


def _make_backend(kind: str):
    import os

    if kind == "scripted":
        return None  # demo.build 会用默认的 ScriptedBackend
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
