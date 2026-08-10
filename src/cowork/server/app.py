"""FastAPI 路由层：端点形状按 M6-界面层接口.md §6，读侧全部调 `cowork.views`。

这一层只做「调 runner/views + 序列化」，没有业务逻辑。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from ..cli import PROVIDERS
from ..llm.effort import LEVELS as EFFORT_LEVELS
from ..store import SqliteStore
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

    # ---------------- 线程 / 任务 ----------------

    @app.get("/api/tasks")
    def list_tasks():
        return runner.list_threads()

    @app.post("/api/tasks", status_code=202)
    async def create_task(req: Request):
        body = await req.json()
        goal = (body.get("goal") or "").strip()
        if not goal:
            return err(400, "goal 不能为空")
        try:
            plan_id = runner.start_plan(goal)
        except RuntimeError as exc:
            return err(400, str(exc))
        return {"plan_id": plan_id}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, after_seq: int = 0):
        detail = runner.get_detail(task_id, after_seq=after_seq)
        return detail if detail is not None else err(404, "没有这个任务")

    @app.post("/api/tasks/{task_id}/intervene", status_code=202)
    async def intervene(task_id: str, req: Request):
        body = await req.json()
        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            return err(400, "instruction 不能为空")
        if not runner.intervene(task_id, instruction):
            return err(409, "任务不在运行中 —— 介入只对运行中的任务生效（下一个 step 边界）")
        return {"accepted": True}

    @app.post("/api/tasks/{task_id}/ruling", status_code=202)
    async def rule_task(task_id: str, req: Request):
        body = await req.json()
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
        body = await req.json()
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
        body = await req.json() if req.headers.get("content-length") else {}
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
                    "verified": p["verified"],
                    "effort": p.get("effort"),
                    "cache": p.get("cache", "unknown"),
                    "configured": configured,
                    "key_hint": hint,
                }
            )
        return out

    @app.put("/api/providers/{name}")
    async def put_provider(name: str, req: Request):
        p = PROVIDERS.get(name)
        if p is None or not p.get("key_env"):
            return err(404, f"未知供应商: {name!r}")
        body = await req.json()
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
            "effort": {
                k: env.get(GLOBAL_KEYS[f"effort.{k}"], v)
                for k, v in DEFAULTS["effort"].items()
            },
        }
        return out

    @app.put("/api/settings")
    async def put_settings(req: Request):
        body = await req.json()
        pairs: dict[str, str] = {}
        for flat, env_name in GLOBAL_KEYS.items():
            section, _, key = flat.partition(".")
            if not key:  # base_url_override 这种顶层项
                if section in body:
                    pairs[env_name] = str(body[section] or "").strip()
                continue
            if section in body and key in body[section]:
                value = str(body[section][key] or "").strip()
                if section == "effort" and value not in EFFORT_LEVELS:
                    return err(400, f"未知推理挡位: {value!r}")
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
