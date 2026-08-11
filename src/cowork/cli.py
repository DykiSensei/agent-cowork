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

from .llm.errors import MissingApiKey


# 每个供应商的端点、key 来源、默认模型分工。
#
# models = (subagent, architect, triage)：Subagent 干活、架构师做决策、分诊走廉价档。
# 一家只有一个型号时三个位置写同一个，不是偷懒 —— §4.1「不同模型干擅长的事」
# 是能力，不是义务。
#
# **这张表会过期**，而且是无声地过期：模型下线时端点通常还在，只是换了 id。
# DeepSeek 的 deepseek-chat → deepseek-v4-flash 就是这么发生的。所以配了
# `python -m cowork.cli models` —— 它拿各家的 GET /v1/models 对一遍这张表，
# 别靠读文档判断这里写的还对不对。
#
# `verified` 记的是这一行**在本机用真 key 打通过**。没打通过的不是错的，
# 是没被验证过的 —— 两者不能混为一谈。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        # v4 起只暴露 deepseek-v4-flash / deepseek-v4-pro（GET /v1/models 实测），
        # deepseek-chat / deepseek-reasoner 只剩别名。三个角色统一 flash：
        # **这等于放弃了「架构师用推理档」**，要拿回来设
        # COWORK_ARCHITECT_MODEL=deepseek-v4-pro。注意 §11.6 / §11.9 / §11.11
        # 的实测数据都出自 deepseek-reasoner，换档后不能直接外推。
        "models": ("deepseek-v4-flash",) * 3,
        "verified": True,
        "effort": "deepseek",
        "cache": "automatic",   # 命中在 usage.prompt_cache_hit_tokens
    },
    "kimi": {
        "base": "https://api.moonshot.cn/v1",
        "key_env": "MOONSHOT_API_KEY",
        "models": ("kimi-k3",) * 3,
        "verified": True,
        "effort": "kimi",
        "cache": "automatic",
    },
    "anthropic": {
        # 唯一走自己 SDK 的一家（llm/anthropic_backend.py），不吃这里的 base
        "base": None,
        "key_env": "ANTHROPIC_API_KEY",
        "models": ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"),
        "verified": False,
        "effort": "anthropic",
        # Anthropic 的缓存是**显式**的：不打 cache_control 断点就一次都不命中
        "cache": "explicit",
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        # gpt-5.6 三档：sol 旗舰 / terra 均衡 / luna 廉价（官方 models 文档）
        "models": ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"),
        "verified": False,
        "effort": "openai",
        # 自动，≥1024 token 的前缀才进缓存；支持 prompt_cache_key 稳定路由
        "cache": "automatic",
        "cache_key": True,
    },
    "gemini": {
        # OpenAI 兼容层，不是原生 /v1beta/models 那套
        "base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "models": ("gemini-3.6-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"),
        "verified": False,
        "effort": "gemini",
        "cache": "automatic",
    },
    "qwen": {
        # 阿里百炼。新文档推 {WorkspaceId}.<region>.maas.aliyuncs.com，
        # 但那个 URL 拼不出通用预设；官方说旧域名仍然可用，所以预设用旧的，
        # 要用工作空间域名就设 COWORK_LLM_BASE_URL。
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        # qwen-max / plus / turbo 是**滚动别名**，自己跟最新版走 ——
        # 对预设来说这比钉死 qwen3.8-max 这种带版本号的更耐放。
        "models": ("qwen-plus", "qwen-max", "qwen-turbo"),
        "verified": False,
        "effort": "qwen",
        "cache": "automatic",
    },
    "zhipu": {
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPUAI_API_KEY",
        "models": ("glm-5", "glm-5", "glm-5-turbo"),
        "verified": False,
        "effort": "zhipu",
        "cache": "automatic",
    },
    "xai": {
        "base": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "models": ("grok-4.5",) * 3,
        "verified": False,
        "effort": "xai",
        "cache": "automatic",
    },
    "doubao": {
        # 火山方舟。Ark 的 model 位历史上要填 endpoint id（ep-xxxx），
        # 现在支持模型名，但版本号带日期后缀且滚动更新 —— 这一行最容易过期，
        # 跑一次 `cowork.cli models doubao` 再用。
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "key_env": "ARK_API_KEY",
        "models": ("doubao-seed-2.0-lite", "doubao-seed-2.0-pro", "doubao-seed-2.0-lite"),
        "verified": False,
        "effort": "doubao",
        "cache": "automatic",
    },
    "litellm": {
        # 自托管代理：保留 virtual key 的预算强制（§10.3）。模型由代理侧决定，
        # 所以三个位置都留空，靠 COWORK_*_MODEL 指定。
        "base": "http://localhost:4000/v1",
        "key_env": "COWORK_LLM_API_KEY",
        "models": (None, None, None),
        "verified": True,
        "cache": "unknown",
    },
}

# 全部供应商名，给 argparse 的 choices 用 —— 加一家只改上面那张表
PROVIDER_NAMES = sorted(PROVIDERS)


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


def available_providers() -> dict[str, str]:
    """这台机器上真的能用的家 → 那家的 Subagent 模型 id（§10.3.3）。

    判据只有一条：**对应的 key 环境变量非空**。这就是「用户填了哪家的 api 就用哪家」
    ——不去 ping 端点（慢且会误判网络问题），也不看 `verified`
    （那记的是「本机验证过模型 id」，是另一件事）。

    `litellm` 不算：它是代理，模型由代理侧决定，`models` 三个位置都是 None，
    没法给出一个「这家的 Subagent 模型」。
    """
    import os

    out: dict[str, str] = {}
    for name, p in PROVIDERS.items():
        sub = p["models"][0]
        if not sub or not p.get("key_env"):
            continue
        if os.environ.get(p["key_env"]):
            out[name] = sub
    return out


def _make_routing_backend(default_kind: str, providers: dict[str, str]):
    """给每个可用供应商建一个后端，包成路由后端。

    只有一家时**不包**——多一层没有意义，而且 `RoutingBackend` 的名字会出现在
    日志和记录里，让人以为发生了跨供应商调度。
    """
    from .llm.routing import RoutingBackend

    default = _make_backend(default_kind)
    others = {k: (default if k == default_kind else _make_backend(k)) for k in providers}
    if len(others) < 2:
        return default
    return RoutingBackend(default, others)


# 会话级 token 护栏（§12 M9）。一次 CLI 调用共享一个 CostGuard ——
# 复核者、路由到别家的 Subagent、架构师是不同的 Backend 对象，但花的是同一笔钱，
# 每个包一个自己的护栏等于没有护栏。`_set_budget()` 由各子命令在开跑前调一次。
_GUARD = None


def _set_budget(limit: int) -> None:
    global _GUARD
    from .llm.budget import CostGuard

    _GUARD = CostGuard(limit) if limit and limit > 0 else None


def budget_note() -> str:
    if _GUARD is None:
        return "会话 token 护栏：关闭"
    return f"会话 token 护栏：{_GUARD.limit}（--budget 0 关闭）"


def _make_backend(kind: str):
    inner = _make_raw_backend(kind)
    if inner is None or _GUARD is None:
        return inner
    from .llm.budget import BudgetedBackend

    return BudgetedBackend(inner, _GUARD)


def _make_raw_backend(kind: str):
    import os

    if kind == "scripted":
        return None  # demo.build / demo_composite.build 会用各自的脚本后端
    if kind not in PROVIDERS:
        raise ValueError(kind)

    p = PROVIDERS[kind]
    sub, arch, triage = p["models"]
    # 挡位（§10.3.2）。默认「架构师 high / Subagent medium / 廉价三件套 off」——
    # 这个分工不是拍的：架构师那几次调用决定整条链的走向，Subagent 是在干活，
    # 而分诊、探查、摘要只判方向（§3.4 / §3.2.1 早就把它们归为廉价档）。
    arch_effort = os.environ.get("COWORK_ARCHITECT_EFFORT", "high")
    sub_effort = os.environ.get("COWORK_SUBAGENT_EFFORT", "medium")
    cheap_effort = os.environ.get("COWORK_CHEAP_EFFORT", "off")
    if kind == "anthropic":
        from .llm.anthropic_backend import AnthropicBackend

        return AnthropicBackend(
            architect_model=os.environ.get("COWORK_ARCHITECT_MODEL", arch),
            triage_model=os.environ.get("COWORK_TRIAGE_MODEL", triage),
            effort=arch_effort,
            subagent_effort=sub_effort,
        )

    from .llm.openai_compat import OpenAICompatBackend

    # base_url / api_key 都显式解析：两家 key 同时存在时，
    # 靠后端内部的回退链会拿错 key（实测踩过）
    return OpenAICompatBackend(
        base_url=os.environ.get("COWORK_LLM_BASE_URL") or p["base"],
        api_key=os.environ.get("COWORK_LLM_API_KEY") or os.environ.get(p["key_env"]),
        subagent_model=os.environ.get("COWORK_SUBAGENT_MODEL", sub),
        architect_model=os.environ.get("COWORK_ARCHITECT_MODEL", arch),
        triage_model=os.environ.get("COWORK_TRIAGE_MODEL", triage),
        # 只有明确支持的一家才带 prompt_cache_key：不认识的字段在严格端点上
        # 会 400，为了一点点路由收益把整条链打挂不划算
        cache_key_supported=bool(p.get("cache_key")),
        # 没声明 effort 方案的（litellm 这种代理）不下发任何思考参数 ——
        # 代理后面是谁不知道，发过去可能 400 也可能被静默吃掉
        effort_profile=p.get("effort"),
        architect_effort=arch_effort,
        subagent_effort=sub_effort,
        cheap_effort=cheap_effort,
        # 只为了没配 key 时能把话说全（"没有配置 deepseek 的 API key…"）
        provider=kind,
    )


def _report_cache(*backends) -> None:
    """把提示词缓存的命中情况打出来。

    放在每个真实后端跑完之后 —— **不打出来的度量等于没有度量**，
    这个项目在 M2 已经吃过一次「参数没人读」的亏（§11.6c 的两个死参数）。
    """
    for b in backends:
        stats = getattr(b, "cache_stats", None)
        if stats is None or not stats.calls:
            continue
        if stats.hit_rate is None:
            print(f"缓存       {getattr(b, 'name', '?')}：{stats.calls} 次调用，"
                  f"这家不报缓存用量", file=sys.stderr)
            continue
        print(
            f"缓存       {getattr(b, 'name', '?')}：命中 {stats.hit_rate:.0%}"
            f"（{stats.cached_tokens:,}/{stats.prompt_tokens:,} 输入 token，"
            f"{stats.calls} 次调用）",
            file=sys.stderr,
        )


def _run_demo(args: argparse.Namespace) -> int:
    _set_budget(args.budget)
    from .demo import build

    lines: list[str] = []
    log = lines.append if args.json else print
    backend = _make_backend(args.backend)
    orch, ws = build(
        args.workspace,
        store=_make_store(args.store),
        backend=backend,
        use_docker=args.docker,
    )
    orch.log = log

    print(f"workspace: {ws}", file=sys.stderr)
    result = orch.run()

    if args.json:
        for ln in lines:
            print(json.dumps({"log": ln}, ensure_ascii=False))
        # 顶层直接摊开 TaskState.to_dict()，**不另起一套字段名**：
        # 界面层会照着这里的键写死解析，同一个东西在 CLI 叫 steps、在轮询接口叫
        # current_step，是白白浪费对面一天的那种不一致（见 M6-界面层接口.md）。
        print(
            json.dumps(
                {
                    **result.state.to_dict(),
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

    if backend is not None:
        _report_cache(backend)
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
    _set_budget(args.budget)
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


def _serve(args: argparse.Namespace) -> int:
    """M6 服务层：HTTP + SSE，顺带挂 ui/dist 的静态文件（先 npm run build）。"""
    try:
        import uvicorn
    except ImportError:
        print("缺依赖：pip install -e .[server]", file=sys.stderr)
        return 2
    from pathlib import Path

    from .server import check_bind_host, create_app, exposure_warning, is_loopback_host

    _set_budget(args.budget)
    # 准入检查在建 app 之前：拒绝要发生在任何端口被占用、任何 key 被读取之前。
    refusal = check_bind_host(args.host, acknowledged=args.i_know_its_exposed)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    if not is_loopback_host(args.host):
        print(exposure_warning(args.host), file=sys.stderr)

    ui_dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    app = create_app(
        db_path=args.db,
        default_backend=args.backend,
        workspace=args.workspace,
        max_cycles=args.max_cycles,
        ui_dist=str(ui_dist) if ui_dist.is_dir() else None,
    )
    print(f"store: {args.db}   backend: {args.backend}   {budget_note()}")
    print(f"UI: {'http://{}:{}'.format(args.host, args.port) if ui_dist.is_dir() else '（ui/dist 不存在，先 cd ui && npm run build）'}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _threads(args: argparse.Namespace) -> int:
    """把某个库里的线程按界面层契约导出（M6 §9 第 3 / 4 条）。

    存在的理由：界面层的 mock 数据现在是手写的，而手写的 mock 会和真实形状
    慢慢分叉。这个命令直接吐 `views.thread_list()` / `views.task_detail()`
    的结果 —— 拿它生成 fixtures，形状就永远是对的。
    """
    from . import views
    from .store import SqliteStore

    store = SqliteStore(args.db)
    if args.task_id:
        detail = views.task_detail(store, args.task_id)
        if detail is None:
            print(f"没有这个任务: {args.task_id}", file=sys.stderr)
            return 1
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return 0

    rows = views.thread_list(store)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        mark = "复合" if r["composite"] else "单任务"
        print(f"{r['status']:<15}{mark:<6}{r['task_id']:<22}{r['title']}")
    return 0


def probe_provider(name: str, *, timeout: float = 10.0) -> dict:
    """探一家供应商：有没有 key、端点通不通、预设的 model id 在不在服务端。

    **CLI 的 `models` 和设置页的「测试连接」共用这一份。** 两套探测迟早会分叉，
    而它们回答的是同一个问题。

    status 的四个取值刻意分开，因为它们的**结论不同**：
      ok         预设的 model id 都在服务端 —— 这家现在能用
      mismatch   端点通、key 有效，但预设写的 id 服务端没有（表过期了）
      unreachable 问不到（网络 / 这家没有这个接口）—— **不代表配置错**
      skipped    没有 key，或这家不吃 /v1/models —— **不代表配置错**
    """
    import os
    import urllib.error
    import urllib.request

    p = PROVIDERS[name]
    wanted = sorted({m for m in p["models"] if m})
    key = os.environ.get("COWORK_LLM_API_KEY") or os.environ.get(p["key_env"] or "")
    if not key:
        return {"name": name, "status": "skipped",
                "detail": f"没有 {p['key_env']}，未验证"}
    if name == "anthropic":
        # 自己的 SDK，模型列表接口也不同 —— 只报「有 key」，不假装验证过
        return {"name": name, "status": "skipped",
                "detail": "走 Anthropic SDK，不吃 /v1/models"}
    if not p["base"]:
        return {"name": name, "status": "skipped", "detail": "没有 base_url"}

    url = p["base"].rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            served = {m["id"] for m in json.load(resp).get("data", [])}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return {"name": name, "status": "unreachable",
                "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}

    missing = [m for m in wanted if m not in served]
    if missing:
        return {"name": name, "status": "mismatch",
                "detail": f"服务端没有 {missing}；实际有 {sorted(served)[:6]}"}
    return {"name": name, "status": "ok", "detail": f"{wanted} 都在服务端"}


_PROBE_LABEL = {"ok": "OK", "mismatch": "对不上", "unreachable": "问不到", "skipped": "跳过"}
_PROBE_EXIT = {"ok": 0, "skipped": 0, "unreachable": 1, "mismatch": 2}
_PROBE_MARK = {"ok": "✓", "mismatch": "✗", "unreachable": "?", "skipped": "-"}
_PROBE_MARK_ASCII = {"ok": "+", "mismatch": "x", "unreachable": "?", "skipped": "-"}


def _marks() -> dict[str, str]:
    """终端编不出 ✓ 就退回 ASCII。

    中文 Windows 的默认控制台是 GBK，`✓` 直接 UnicodeEncodeError —— 而这是个
    **查状态**的命令，第一次跑的人拿它确认环境，结果它自己崩了。
    装饰字符不值得让一条诊断命令挂掉。
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗".encode(enc)
    except (UnicodeEncodeError, LookupError):
        return _PROBE_MARK_ASCII
    return _PROBE_MARK


def _models(args: argparse.Namespace) -> int:
    """拿各家的 GET /v1/models 对一遍 PROVIDERS 表。

    存在的理由：**这张表会无声地过期**。模型下线时端点还在、key 还有效，
    只是那个 id 不再被服务，报错要等到第一次真实调用（而且报出来的往往是
    404 model_not_found 之外的别的东西）。DeepSeek 的 deepseek-chat →
    deepseek-v4-flash 就是这么发生的。所以给一个能直接问的命令，
    别靠读文档判断表里写的还对不对。
    """
    names = [args.provider] if args.provider else PROVIDER_NAMES
    rows: list[tuple[str, str, str]] = []
    exit_code = 0

    marks = _marks()
    for name in names:
        r = probe_provider(name, timeout=args.timeout)
        rows.append((marks[r["status"]], name, _PROBE_LABEL[r["status"]], r["detail"]))
        exit_code = max(exit_code, _PROBE_EXIT[r["status"]])

    width = max(len(r[1]) for r in rows)
    for mark, name, status, detail in rows:
        print(f"{mark} {name:<{width}}  {status:<6} {detail}")
    print("\n「跳过」不代表配置错，只代表这次没验证到 —— 缺 key 或那家没有这个接口。",
          file=sys.stderr)

    _print_environment(marks)
    return exit_code


def _print_environment(marks: dict[str, str]) -> None:
    """供应商之外的自检：.env 从哪读的、可选组件在不在、现在能跑什么。

    这半是给**第一次跑的人**的 —— 供应商详情设置页做得更好，
    但「Docker 守护进程在不在」「ui/dist 建了没有」没有别的地方会说。
    """
    import os
    import shutil
    import socket
    from pathlib import Path

    from .config import find_env_file

    def _port_open(host: str, port: int) -> bool:
        with socket.socket() as sk:
            sk.settimeout(0.4)
            return sk.connect_ex((host, port)) == 0

    # 表格走 stdout、这一段走 stderr，两个流的缓冲不同步 —— 被管道接走时
    # 顺序会颠倒。先把前面的刷出去。
    sys.stdout.flush()

    env_file = find_env_file()
    root = Path(__file__).resolve().parents[2]
    dist = root / "ui" / "dist"
    providers = available_providers()

    checks = [
        (".env", bool(env_file and Path(env_file).is_file()),
         str(env_file) if env_file else "没找到（从 .env.example 复制一份）"),
        ("Postgres", _port_open("127.0.0.1", 5433),
         "localhost:5433（只有 --store pg 需要）"),
        ("LiteLLM", _port_open("127.0.0.1", 4000),
         "localhost:4000（只有要 virtual key 预算强制时需要）"),
        ("Docker", shutil.which("docker") is not None,
         "只有 --docker 沙箱需要"),
        ("ui/dist", dist.is_dir(), "cd ui && npm install && npm run build（serve 要）"),
    ]
    print("\n环境：", file=sys.stderr)
    for label, ok, detail in checks:
        mark = marks["ok"] if ok else marks["skipped"]
        print(f"  {mark} {label:<10} {detail}", file=sys.stderr)

    print("\n你现在可以跑：", file=sys.stderr)
    print("  · cowork demo                     不需要 key，完整链路走一遍", file=sys.stderr)
    if providers:
        first = next(iter(providers))
        print(f"  · cowork demo --backend {first}{' ' * max(0, 10 - len(first))}"
              f"真实模型", file=sys.stderr)
        print(f'  · cowork plan "<一句话目标>" --run   拆解 + 并行执行', file=sys.stderr)
    else:
        print("  （还没有任何供应商的 key —— 填一个才能用真实模型：",
              file=sys.stderr)
        print("    写进 .env，或跑 cowork serve 在设置页里填）", file=sys.stderr)
    if dist.is_dir():
        print("  · cowork serve                    带界面跑", file=sys.stderr)


def _plan(args: argparse.Namespace) -> int:
    """从一个自然语言目标拆出可派发的子任务（§12 M7 7.3 / 7.4）。"""
    _set_budget(args.budget)
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

    _report_cache(architect.backend, architect.reviewer_backend)
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

    # 从自然语言目标一路跑到产出：拆解 → 分层 → 选模型 → 并行执行（§12 M7 出口 1）。
    # 复核已经在 plan() 里做过了，所以这里 Scheduler 不再给 root_goal ——
    # 否则同一份拆解会被复核两次，白花一次调用。
    from .scheduler import Scheduler

    # 并行度和分工已经定了，最后一步才轮到「谁来干」（§10.3.3）。
    # 放在这个位置是有讲究的：拆解定型之前问「用哪家」，人手上没有可判断的依据。
    providers = available_providers()
    specs, _profiles = architect.assign_models(result.specs, providers, log=print)
    exec_backend = _make_routing_backend(args.backend, providers) if len(providers) > 1 \
        else architect.backend

    print("\n" + "=" * 60)
    sched = Scheduler(
        specs,
        backend=exec_backend,
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
    used = getattr(exec_backend, "used", None)
    if used:
        # 跑完要能说清「这次实际是谁在干活」—— 分配是人做的决定，
        # 而人做过的决定必须看得见结果（§7.3 的同一条理由）
        print(f"实际分工   {dict(sorted(used.items()))}")
    _report_cache(exec_backend)
    return 0 if outcome.completed else 1


def _bench_plan(args: argparse.Namespace) -> int:
    import tempfile
    import time
    from pathlib import Path

    from .bench.plan_ab import NAIVE_DECOMPOSE_SYSTEM, run_batch, select_goals

    goals = select_goals(args.goals)
    if not goals:
        print(f"--goals {args.goals!r} 没匹配到任何目标", file=sys.stderr)
        return 2

    def _with_prompt(system: str | None):
        def factory():
            backend = _make_backend(args.backend)
            if system is not None:
                backend.decompose_system = system
            return backend
        return factory

    arms = {"full": _with_prompt(None), "naive": _with_prompt(NAIVE_DECOMPOSE_SYSTEM)}
    if args.arms:
        wanted = {x.strip() for x in args.arms.split(",")}
        arms = {k: v for k, v in arms.items() if k in wanted}

    reviewer = resolve_reviewer(args.backend, args.reviewer)
    root = Path(args.workspace or tempfile.mkdtemp(prefix="cowork-planbench-"))
    total = len(goals) * len(arms) * args.repeat
    print(f"目标 {len(goals)} × arm {len(arms)} × {args.repeat} 次 = {total} 次拆解，"
          f"拆解者 {args.backend} / 复核者 {reviewer or '（同拆解者）'}", file=sys.stderr)

    started = time.monotonic()

    def progress(rec, done: int, total_: int) -> None:
        eta = (time.monotonic() - started) / done * (total_ - done)
        note = "ERROR" if rec.error else (
            f"{rec.status} {rec.attempts}轮 "
            + ("一轮过" if rec.first_round_clean else f"被驳回{'→救回' if rec.recovered else ''}")
        )
        print(f"[{done}/{total_}] {rec.goal_id}/{rec.arm} {note} token={rec.tokens} "
              f"{rec.wall_seconds:.0f}s ETA {eta / 60:.1f}min", file=sys.stderr)

    out = Path(args.out)
    run_batch(
        goals, arms=arms,
        reviewer_factory=(lambda: _make_backend(reviewer)) if reviewer else None,
        repeat=args.repeat, out_path=out, workspace_root=root,
        workers=args.workers, progress=progress,
    )
    print(f"\n记录写入 {out}", file=sys.stderr)
    return _bench_plan_report(argparse.Namespace(records=str(out), json=False))


def _bench_plan_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .bench.plan_ab import load, render, summarize

    summary = summarize(load(Path(args.records)))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0


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


def _bench_decide(args: argparse.Namespace) -> int:
    """写入侧复核对照（§12 M8）。判别力不能从 7.2 外推，所以必须单独测。"""
    import time
    from pathlib import Path

    from .bench.decide_ab import run_batch, select_cases

    if args.backend == "scripted":
        print("写入侧复核对照需要真实模型：脚本后端没有语义判断力，测出来的是自证的假数据",
              file=sys.stderr)
        return 2

    cases = select_cases(args.cases)
    arms = {
        name: (lambda n=name: _make_backend(n))
        for name in [x.strip() for x in args.arms.split(",") if x.strip()]
    }
    if not arms:
        print("--arms 不能为空", file=sys.stderr)
        return 2

    total = len(cases) * len(arms) * args.repeat
    n_unsound = sum(1 for c in cases if not c.sound)
    print(f"用例 {len(cases)} 个（正例 {n_unsound} / 负例 {len(cases) - n_unsound}）"
          f" × arm {len(arms)} × {args.repeat} 次 = {total} 次复核调用，arm={list(arms)}",
          file=sys.stderr)

    started = time.monotonic()

    def progress(rec, done: int, total_: int) -> None:
        elapsed = time.monotonic() - started
        eta = elapsed / done * (total_ - done)
        got = "ERROR" if rec.error else ("报问题" if rec.flagged else "放行")
        want = "该放行" if rec.sound else f"该报（{rec.defect}）"
        hit = "" if rec.error else ("✓" if rec.flagged != rec.sound else "✗")
        print(f"[{done}/{total_}] {rec.case_id}({want}) arm={rec.arm} -> {got} {hit} "
              f"token={rec.tokens} {rec.wall_seconds:.1f}s ETA {eta / 60:.1f}min",
              file=sys.stderr)

    out = Path(args.out)
    run_batch(cases, arms=arms, repeat=args.repeat, out_path=out,
              workers=args.workers, progress=progress)
    print(f"\n记录写入 {out}", file=sys.stderr)
    return _bench_decide_report(argparse.Namespace(records=str(out), json=False))


def _bench_decide_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .bench.decide_ab import load, render, summarize

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
        choices=["scripted", *PROVIDER_NAMES],
        default="scripted",
        help=(
            "anthropic 需 ANTHROPIC_API_KEY；deepseek 需 DEEPSEEK_API_KEY；"
            "kimi 需 MOONSHOT_API_KEY；openai 走 COWORK_LLM_BASE_URL（默认 LiteLLM）"
        ),
    )
    d.add_argument("--docker", action="store_true", help="用 Docker 沙箱跑工具调用")
    d.add_argument("--budget", type=int, default=1_000_000, dest="budget",
                   help="会话 token 硬上限（应用层，不依赖 LiteLLM）。0 = 关闭")
    d.set_defaults(func=_run_demo)

    m = sub.add_parser("models", help="拿各家的 /v1/models 对一遍 PROVIDERS 表")
    m.add_argument("provider", nargs="?", choices=PROVIDER_NAMES, default=None)
    m.add_argument("--timeout", type=float, default=15.0)
    m.set_defaults(func=_models)

    th = sub.add_parser("threads", help="按界面层契约导出线程（M6）")
    th.add_argument("db", help="SQLite 库路径")
    th.add_argument("task_id", nargs="?", default=None,
                    help="给了就出这条线程的详情（含时间线），不给就出列表")
    th.add_argument("--json", action="store_true", help="列表也用 JSON")
    th.set_defaults(func=_threads)

    s = sub.add_parser("serve", help="M6 服务层：HTTP + SSE + 静态 UI")
    s.add_argument("--host", default="127.0.0.1",
                   help="只绑 loopback 是刻意的：没有权限概念（接口文档 §6）。"
                        "非回环地址会被拒绝，除非同时给 --i-know-its-exposed")
    s.add_argument("--i-know-its-exposed", action="store_true",
                   dest="i_know_its_exposed",
                   help="确认要把一个无认证、能读写 API key 的服务绑到回环之外")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default="cowork.sqlite", help="SQLite 库路径")
    s.add_argument("--backend", choices=PROVIDER_NAMES, default="deepseek")
    s.add_argument("--workspace", default=None,
                   help="任务 workspace 根目录（默认每次拆解一个临时目录）")
    s.add_argument("--max-cycles", type=int, default=8, dest="max_cycles")
    s.add_argument("--budget", type=int, default=1_000_000, dest="budget",
                   help="token 硬上限（应用层，不依赖 LiteLLM）。0 = 关闭。"
                        "**服务是长驻的，这个额度跨整个进程生命周期、用完要重启** —— 它防的正是「跑一夜把余额烧光」")
    s.set_defaults(func=_serve)

    i = sub.add_parser("inspect", help="导出某个 SQLite 库里的任务与决策")
    i.add_argument("db")
    i.set_defaults(func=_inspect)

    c = sub.add_parser("composite", help="复合任务演示：并行 + 冲突检测（§12 M4）")
    c.add_argument("--workspace", default=None)
    c.add_argument("--json", action="store_true")
    c.add_argument("--store", choices=["sqlite", "pg"], default="sqlite")
    c.add_argument("--backend", choices=["scripted", *PROVIDER_NAMES],
                   default="scripted")
    c.add_argument("--reviewer",
                   choices=["auto", "none", *PROVIDER_NAMES],
                   default="auto",
                   help=f"拆解复核的供应商（§12 M7 7.1）。auto：真实后端时用 "
                        f"{DEFAULT_REVIEWER}、脚本后端时不复核；none 退回同模型复核")
    c.add_argument("--max-cycles", type=int, default=4, dest="max_cycles")
    c.add_argument("--budget", type=int, default=1_000_000, dest="budget",
                   help="会话 token 硬上限（应用层，不依赖 LiteLLM）。0 = 关闭")
    c.set_defaults(func=_run_composite)

    b = sub.add_parser("bench", help="M2 参数实测跑批（§12 M2）")
    b.add_argument("--backend", choices=PROVIDER_NAMES,
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
    pl.add_argument("--backend", choices=PROVIDER_NAMES,
                    default="deepseek", help="拆解者")
    pl.add_argument("--reviewer",
                    choices=["auto", "none", *PROVIDER_NAMES],
                    default="auto", help=f"复核者，auto = {DEFAULT_REVIEWER}")
    pl.add_argument("--gate", choices=["auto", "cli"], default="auto",
                    help="升级给人时谁来答：auto 自动放行，cli 你自己在终端拍板")
    pl.add_argument("--workspace", default=None)
    pl.add_argument("--store", choices=["sqlite", "pg"], default="sqlite")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--run", action="store_true",
                    help="拆解通过后直接派发执行 —— 从自然语言目标一路跑到产出。花真钱")
    pl.add_argument("--max-cycles", type=int, default=4, dest="max_cycles")
    pl.add_argument("--budget", type=int, default=1_000_000, dest="budget",
                   help="会话 token 硬上限（应用层，不依赖 LiteLLM）。0 = 关闭")
    pl.set_defaults(func=_plan)

    rv = sub.add_parser("bench-review", help="跨模型复核对照实测（§12 M7 7.2）")
    rv.add_argument("--backend", choices=PROVIDER_NAMES,
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

    bd = sub.add_parser("bench-decide", help="写入侧复核对照实测（§12 M8）")
    bd.add_argument("--backend", choices=PROVIDER_NAMES, default="deepseek",
                    help="只用来挡住 scripted；复核者由 --arms 决定")
    bd.add_argument("--arms", default="deepseek,kimi",
                    help="逗号分隔的复核者供应商")
    bd.add_argument("--repeat", type=int, default=5,
                    help="每个用例每个 arm 跑几次。低于 5 的结果是噪声（§11.5d）")
    bd.add_argument("--workers", type=int, default=4)
    bd.add_argument("--cases", default=None,
                    help="逗号分隔的用例 id / 家族名 / 缺陷形态 / sound / unsound")
    bd.add_argument("--out", default="decide_ab.jsonl")
    bd.set_defaults(func=_bench_decide)

    bdr = sub.add_parser("bench-decide-report", help="只出写入侧复核报告，不重跑")
    bdr.add_argument("records")
    bdr.add_argument("--json", action="store_true")
    bdr.set_defaults(func=_bench_decide_report)

    bp = sub.add_parser("bench-plan",
                        help="拆解提示词对照 + 生成-复核循环实测（§12 M7 7.4 / 风险 #17）")
    bp.add_argument("--backend", choices=PROVIDER_NAMES,
                    default="deepseek", help="拆解者")
    bp.add_argument("--reviewer",
                    choices=["auto", "none", *PROVIDER_NAMES],
                    default="auto")
    bp.add_argument("--arms", default=None, help="full,naive 的子集，默认两个都跑")
    bp.add_argument("--goals", default=None, help="逗号分隔的目标 id，默认全跑")
    bp.add_argument("--repeat", type=int, default=2)
    bp.add_argument("--workers", type=int, default=3)
    bp.add_argument("--workspace", default=None)
    bp.add_argument("--out", default="plan_ab.jsonl")
    bp.set_defaults(func=_bench_plan)

    bpr = sub.add_parser("bench-plan-report", help="只出拆解对照报告，不重跑")
    bpr.add_argument("records")
    bpr.add_argument("--json", action="store_true")
    bpr.set_defaults(func=_bench_plan_report)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except MissingApiKey as exc:
        # 配置没做完不是程序错误 —— 给一段照着做就行的话，别给 traceback。
        # 第一次跑的人看到 40 行调用栈会以为是这个项目坏了。
        print(f"\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
