"""FastAPI 路由层：端点形状按 M6-界面层接口.md §6，读侧全部调 `cowork.views`。

这一层只做「调 runner/views + 序列化」，没有业务逻辑。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from ..cli import PROVIDERS, available_providers
from ..llm.effort import LEVELS as EFFORT_LEVELS
from ..store import SqliteStore
from ..workspace import browse as browse_dirs
from ..workspace import resolve_workspace
from .runner import Runner
from .settings_io import DEFAULTS, GLOBAL_KEYS, effective_env, key_hint, update_env
from .tap import EventHub, TapStore

# fastapi 是可选依赖（pip install -e .[server]）。在模块级 guarded import 而不是
# create_app 里导入，是因为 FastAPI 要用模块全局命名空间解析路由函数的类型标注
# （闭包里的 import 它看不到，会把 Request 当成 query 参数，422）。
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    FastAPI = Request = JSONResponse = StreamingResponse = StaticFiles = None  # type: ignore[assignment]
    _HAS_FASTAPI = False


def create_app(
    *,
    store=None,
    db_path: str = "cowork.sqlite",
    default_backend: str = "deepseek",
    workspace: str | None = None,
    max_cycles: int = 8,
    backend_factory=None,
    ui_dist: str | None = None,
):
    if not _HAS_FASTAPI:
        raise ImportError("缺依赖：pip install -e .[server]")

    hub = EventHub()
    tapped = TapStore(store or SqliteStore(db_path), hub)
    runner = Runner(
        tapped,
        hub,
        default_backend=default_backend,
        workspace=workspace,
        max_cycles=max_cycles,
        backend_factory=backend_factory,
    )

    @asynccontextmanager
    async def lifespan(_app):
        hub.bind(asyncio.get_running_loop())
        yield

    app = FastAPI(title="cowork-server", lifespan=lifespan)
    # 暴露给测试与调试： runner 持有注册表（活任务 / plans）和 tapped store
    app.state.runner = runner
    app.state.hub = hub

    def err(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    async def optional_body(req) -> dict:
        """body 可选的端点用这个（契约里写成 `{reason?}` 的那些）。

        原来的判据是 `req.headers.get("content-length")` —— 那是个**字符串**，
        `"0"` 为真，于是 `Content-Length: 0`（curl -X POST 的默认形态）会去
        解析空 body，抛出去就是 500。契约说可选，实现就得真的可选。
        """
        try:
            raw = await req.body()
        except Exception:  # noqa: BLE001 - 连接断了，当成没给 body
            return {}
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ---------------- 线程 / 任务 ----------------

    @app.get("/api/tasks")
    def list_tasks():
        return runner.list_threads()

    @app.post("/api/tasks", status_code=202)
    async def create_task(req: Request):
        # 全部入口都走 optional_body：body 不合法时该回 400（下面的字段校验会说
        # 清楚缺什么），不该是 500
        body = await optional_body(req)
        goal = (body.get("goal") or "").strip()
        if not goal:
            return err(400, "goal 不能为空")
        mode = (body.get("mode") or "new").strip()
        if mode not in ("new", "takeover"):
            return err(400, f"mode 只能是 new / takeover，收到 {mode!r}")
        try:
            plan_id = runner.start_plan(
                goal,
                ws=(body.get("workspace") or "").strip() or None,
                takeover=mode == "takeover",
            )
        except ValueError as exc:      # 工作区路径不能用（WorkspaceError）
            return err(400, str(exc))
        except RuntimeError as exc:    # 没配 key 之类
            return err(400, str(exc))
        return {"plan_id": plan_id}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, after_seq: int = 0):
        detail = runner.get_detail(task_id, after_seq=after_seq)
        return detail if detail is not None else err(404, "没有这个任务")

    @app.post("/api/tasks/{task_id}/intervene", status_code=202)
    async def intervene(task_id: str, req: Request):
        body = await optional_body(req)
        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            return err(400, "instruction 不能为空")
        if not runner.intervene(task_id, instruction):
            # 文案是给**用户**看的，不是给我们自己看的：「step 边界」是这套系统
            # 内部的说法，用户没有理由知道它。说清楚三件事就够 ——
            # 现在是什么状态、这条消息去哪了、接下来该干什么。
            return err(
                409,
                "这个任务现在没有在跑，所以这句话没有送出去。"
                "如果它在等你拍板，请在上方的卡片里选一个处理方式；"
                "如果它已经结束了，可以发布一个新任务。",
            )
        return {"accepted": True}

    @app.post("/api/tasks/{task_id}/cancel", status_code=202)
    async def cancel_task(task_id: str, req: Request):
        body = await optional_body(req)
        if not runner.cancel(task_id, (body.get("reason") or "").strip()):
            # 挂起的任务不在这条路上 —— 那是 ruling(ABANDON)，提示里说清楚，
            # 否则界面只能给用户一个「409」
            return err(
                409,
                "这个任务现在没有在跑，没什么可停的。"
                "如果它在等你拍板，在上方卡片里选「放弃这个任务」；"
                "如果它已经结束了，就不用停了。",
            )
        return {"accepted": True}

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        """删掉一条线程。**只删记录，不碰工作区里的文件。**

        产物是人的东西 —— 删一条聊天记录不该顺手删掉他的代码。要删文件人自己
        去那个目录删（界面上显示着路径）。
        """
        ok, why = runner.delete_thread(task_id)
        return {"deleted": True} if ok else err(409, why)

    @app.get("/api/fs")
    def browse_fs(path: str = ""):
        """列目录，给界面上的文件夹选择器用（浏览器拿不到本机绝对路径）。"""
        try:
            return browse_dirs(path)
        except ValueError as exc:
            return err(400, str(exc))

    @app.post("/api/tasks/{task_id}/ruling", status_code=202)
    async def rule_task(task_id: str, req: Request):
        body = await optional_body(req)
        action = (body.get("action") or "").strip()
        if action not in ("CONTINUE", "MODIFY_TASK", "ABANDON", "REASSIGN"):
            return err(400, f"未知 action: {action!r}")
        try:
            runner.rule_task(
                task_id,
                action=action,
                rationale=body.get("rationale") or "",
                spec_changes=body.get("spec_changes"),
            )
        except KeyError:
            return err(404, "没有这个任务")
        except ValueError as exc:
            return err(409, str(exc))
        except RuntimeError as exc:  # 没配 key 之类，restore 起不来
            return err(400, str(exc))
        return {"accepted": True}

    # ---------------- 拆解 ----------------

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: str):
        plan = runner.get_plan(plan_id)
        return plan if plan is not None else err(404, "没有这次拆解")

    @app.post("/api/plans/{plan_id}/ruling")
    async def rule_plan(plan_id: str, req: Request):
        body = await optional_body(req)
        if "accept" not in body and not body.get("specs"):
            # 缺字段不能落到 accept=False 上 —— 那会把一次畸形请求变成「人否决了
            # 这份拆解」，而否决是有后果的
            return err(400, "要么给 accept（true/false），要么给一份 specs")
        try:
            runner.rule_plan(
                plan_id,
                accept=bool(body.get("accept")),
                rationale=body.get("rationale") or "",
                specs=body.get("specs"),
            )
        except KeyError:
            return err(404, "没有这次拆解")
        except ValueError as exc:
            return err(409, str(exc))
        return {"ok": True}

    @app.post("/api/plans/{plan_id}/dispatch", status_code=202)
    async def dispatch_plan(plan_id: str, req: Request):
        body = await optional_body(req)
        try:
            root_id = runner.dispatch(plan_id, body.get("assignments"))
        except KeyError:
            return err(404, "没有这次拆解")
        except ValueError as exc:
            return err(409, str(exc))
        except RuntimeError as exc:
            return err(400, str(exc))
        return {"root_id": root_id}

    # ---------------- SSE ----------------

    @app.get("/api/stream")
    async def stream():
        async def gen():
            q = hub.subscribe()
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # ---------------- 设置页 ----------------

    @app.get("/api/providers")
    def list_providers():
        out = []
        for name, p in sorted(PROVIDERS.items()):
            configured, hint = key_hint(p["key_env"]) if p.get("key_env") else (False, None)
            out.append(
                {
                    "name": name,
                    "base": p["base"],
                    "key_env": p["key_env"],
                    "models": {
                        "subagent": p["models"][0],
                        "architect": p["models"][1],
                        "triage": p["models"][2],
                    },
                    # **这是「我们验证过这个预设」，不是「你的 key 有效」。**
                    # 两件事在界面上曾经共用一个「已验证 / 未验证」标签，
                    # 用户填完自己的 key 看到「未验证」会以为是自己填错了。
                    "preset_verified": p["verified"],
                    "verified": p["verified"],  # 兼容旧前端，勿新用
                    "effort": p.get("effort"),
                    "cache": p.get("cache", "unknown"),
                    "configured": configured,
                    "key_hint": hint,
                }
            )
        return out

    @app.post("/api/providers/{name}/test")
    async def test_provider(name: str):
        """真打一次端点，回答「这个 key 现在能不能用」。

        设置页原来只显示「已配置」，判据是环境变量非空 —— 填错一个 key 照样
        显示已配置，任务照样 401。这个端点和 `cli models` 共用
        `probe_provider()`：两套探测迟早会分叉，而它们回答的是同一个问题。
        """
        if name not in PROVIDERS:
            return err(404, f"未知供应商: {name!r}")
        from ..cli import probe_provider

        # 探测要打网络，别占住事件循环
        return await asyncio.to_thread(probe_provider, name)

    @app.put("/api/providers/{name}")
    async def put_provider(name: str, req: Request):
        p = PROVIDERS.get(name)
        if p is None or not p.get("key_env"):
            return err(404, f"未知供应商: {name!r}")
        body = await optional_body(req)
        try:
            update_env({p["key_env"]: (body.get("api_key") or "").strip()})
        except ValueError as exc:
            # 值里带换行会往 .env 多写一行 —— 那是任意环境变量注入，不是 500
            return err(400, str(exc))
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings():
        env = effective_env()
        out = {
            "base_url_override": env.get(GLOBAL_KEYS["base_url_override"], ""),
            "models": {
                k: env.get(GLOBAL_KEYS[f"models.{k}"], v)
                for k, v in DEFAULTS["models"].items()
            },
            "providers": {
                k: env.get(GLOBAL_KEYS[f"providers.{k}"], v)
                for k, v in DEFAULTS["providers"].items()
            },
            "effort": {
                k: env.get(GLOBAL_KEYS[f"effort.{k}"], v)
                for k, v in DEFAULTS["effort"].items()
            },
            "review_writes": env.get(
                GLOBAL_KEYS["review_writes"], DEFAULTS["review_writes"]
            ),
            "workspace": env.get(GLOBAL_KEYS["workspace"], ""),
            # 界面要显示「没配的话东西会落在哪」—— 这个问题得有答案
            "workspace_default": str(runner.workspace_root()),
            "allowed_binaries": env.get(
                GLOBAL_KEYS["allowed_binaries"], DEFAULTS["allowed_binaries"]
            ),
            "allow_network": env.get(
                GLOBAL_KEYS["allow_network"], DEFAULTS["allow_network"]
            ),
        }
        return out

    @app.put("/api/settings")
    async def put_settings(req: Request):
        body = await optional_body(req)
        pairs: dict[str, str] = {}
        for flat, env_name in GLOBAL_KEYS.items():
            section, _, key = flat.partition(".")
            if not key:  # base_url_override / review_writes 这种顶层项
                if section not in body:
                    continue
                raw = body[section]
                if flat == "review_writes":
                    # **必须收字符串 on/off，不能收布尔**：`str(False or "")` 是
                    # 空串，而空串在 .env 语义里是「未设置」→ 回落到默认（开）。
                    # 也就是说前端发 false 反而关不掉。
                    value = str(raw).strip().lower()
                    if value not in ("on", "off"):
                        return err(400, f"review_writes 只能是 on / off，收到 {raw!r}")
                    pairs[env_name] = value
                elif flat == "allow_network":
                    value = str(raw).strip().lower()
                    if value not in ("on", "off"):
                        return err(400, f"allow_network 只能是 on / off，收到 {raw!r}")
                    pairs[env_name] = value
                elif flat == "workspace":
                    value = str(raw or "").strip()
                    if value:
                        # 路径不能用要当场说，别等到下一个任务起跑才炸
                        try:
                            value = str(resolve_workspace(value))
                        except ValueError as exc:
                            return err(400, str(exc))
                    pairs[env_name] = value
                else:
                    pairs[env_name] = str(raw or "").strip()
                continue
            if section in body and key in body[section]:
                value = str(body[section][key] or "").strip()
                if section == "effort" and value not in EFFORT_LEVELS:
                    return err(400, f"未知推理挡位: {value!r}")
                if section == "providers" and value:
                    # 只能选**已经配了 key 的**那几家：选一家没 key 的，
                    # 任务会在起跑时才失败，而那时人已经离开设置页了。
                    # 复核者多一个 none（明确关掉独立复核，退回同模型）。
                    allowed = set(available_providers())
                    if key == "reviewer":
                        allowed.add("none")
                    if value not in allowed:
                        return err(
                            400,
                            f"{value!r} 这家还没配 API key（可选：{sorted(allowed)}）",
                        )
                pairs[env_name] = value
        if pairs:
            try:
                update_env(pairs)
            except ValueError as exc:
                return err(400, str(exc))
        return {"ok": True}

    # ---------------- 静态 UI（最后挂，/api 优先） ----------------

    if ui_dist:
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    return app
