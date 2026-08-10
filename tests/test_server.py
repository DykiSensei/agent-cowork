"""M6 服务层：HTTP 端点 + restore 路径的端到端。

fastapi / httpx 不是默认依赖（`pip install -e .[server]`），缺了就整个模块
skip —— 和 PG / LiteLLM / Docker 那几组 skip 同一待遇。
全部用脚本后端跑：确定性、不花钱、不需要 key。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from cowork.actions import Finish, ToolCall
from cowork.llm import ArchitectVerdict, SubtaskDraft
from cowork.llm.scripted import ScriptedBackend
from cowork.orchestrator import Orchestrator
from cowork.server import create_app  # 模块级不引 fastapi，只有调用时才需要
from cowork.store import SqliteStore
from cowork.types import (
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskStatus,
)

QUIET = lambda _: None


def _wait_for(predicate, timeout: float = 25.0, what: str = "条件"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(0.2)
    raise AssertionError(f"等{what}超时（{timeout}s）")


def _restore_scenario(tmp: Path):
    """一个会先挂起、裁决后能跑完的任务。

    第一次运行：Finish 时验收命令发现 ok.txt 不存在 -> TEST_FAILED ->
    裁决 ABANDON -> 升级（ABANDON 必升级）-> 没人答 -> AWAITING_HUMAN。
    恢复之后：write_file 补上 ok.txt -> 验收通过 -> COMPLETED。
    """
    check = (
        "import pathlib,sys;"
        "sys.exit(0 if pathlib.Path('ok.txt').exists() else 1)"
    )
    spec = TaskSpec(
        goal="在 workspace 里造出 ok.txt",
        acceptance=[
            Criterion(
                id="c1",
                description="ok.txt 存在",
                command=["python", "-c", check],
            )
        ],
        task_class=TaskClass.CODE,
        sandbox=SandboxProfile(
            workspace=str(tmp), allowed_binaries=("python",)
        ),
        scope=["ok.txt"],
        tools=["write_file", "read_file", "list_files", "run"],
        max_steps=8,
        deadline_s=120.0,
        token_budget=50_000,
    )
    steps = {
        (1, 0): Finish(output={"file": "ok.txt"}),
        (1, 1): ToolCall("write_file", {"path": "ok.txt", "content": "ok"}),
        (1, 2): Finish(output={"file": "ok.txt"}),
    }
    verdict = ArchitectVerdict(
        action="ABANDON", rationale="脚本裁决：建议放弃", complexity_score=0.9
    )
    backend = ScriptedBackend(steps, verdict_for=lambda _s, _sig: verdict)
    return spec, backend


@unittest.skipIf(TestClient is None, "fastapi/httpx 未安装（pip install -e .[server]）")
class TestServer(unittest.TestCase):
    def _app(self, backend, tmp: Path):
        store = SqliteStore(":memory:")
        app = create_app(
            store=store,
            backend_factory=lambda: backend,
            workspace=str(tmp),
        )
        return TestClient(app)

    # ---------------------------------------------------------- #
    # restore 路径：挂起 -> 人裁决 -> 恢复 -> 跑完
    # ---------------------------------------------------------- #

    def test_restore_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            spec, backend = _restore_scenario(tmp / "ws")
            (tmp / "ws").mkdir()
            client = self._app(backend, tmp)
            with client:
                runner = client.app.state.runner

                # 第一次跑（直接驱动，不经过 HTTP —— 创建路径由 plan 流程覆盖）
                orch = Orchestrator(
                    spec,
                    backend=backend,
                    store=runner.store,
                    human_gate=runner.gate,
                    log=QUIET,
                )
                result = orch.run()
                self.assertIs(result.state.status, TaskStatus.AWAITING_HUMAN)

                # 列表里能按最坏状态看见它，详情里 pending 带着系统建议
                threads = client.get("/api/tasks").json()
                row = next(t for t in threads if t["task_id"] == spec.id)
                self.assertEqual(row["status"], "AWAITING_HUMAN")
                detail = client.get(f"/api/tasks/{spec.id}").json()
                self.assertEqual(detail["kind"], "single")
                pending = detail["pending"]
                self.assertIsNotNone(pending)
                self.assertEqual(pending["suggestion"]["action"], "ABANDON")
                self.assertTrue(pending["checkpoint_id"])

                # 正在运行的任务不能裁决？—— 它不在运行；但 COMPLETED 的不能裁决
                # 先验证 409 的那一面（对 AWAITING 的任务重复裁决由恢复后状态挡住）

                # 人答复：继续 -> restore -> 跑完
                r = client.post(
                    f"/api/tasks/{spec.id}/ruling",
                    json={"action": "CONTINUE", "rationale": "再试一次"},
                )
                self.assertEqual(r.status_code, 202, r.text)

                def done():
                    d = client.get(f"/api/tasks/{spec.id}").json()
                    return d if d["state"]["status"] == "COMPLETED" else None

                final = _wait_for(done, what="任务跑到 COMPLETED")
                self.assertEqual(final["state"]["status"], "COMPLETED")
                self.assertTrue((tmp / "ws" / "ok.txt").is_file())

                # 裁决留痕：占位一条 + 人的裁决一条，建议与升级原因都还在
                decisions = list(final["decisions"].values())
                human = [d for d in decisions if d["decider"] == "HUMAN" and d["resume_mode"]]
                self.assertEqual(len(human), 1)
                self.assertEqual(human[0]["action"], "CONTINUE")
                self.assertIsNotNone(human[0]["escalation_reason"])
                self.assertEqual(human[0]["suggestion"]["action"], "ABANDON")
                self.assertEqual(human[0]["rationale"], "再试一次")

    # ---------------------------------------------------------- #
    # plan -> dispatch 主流程
    # ---------------------------------------------------------- #

    def test_plan_dispatch_flow(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            steps = {(1, 0): Finish(output={"file": "a.txt"})}
            verdict = ArchitectVerdict(
                action="CONTINUE", rationale="脚本", complexity_score=0.1
            )

            def decompose(goal, _feedback):
                return [
                    SubtaskDraft(
                        id="t1_build",
                        goal="造出 a.txt",
                        acceptance=[{"id": "c1", "description": "a.txt 被造出来"}],
                        scope=["a.txt"],
                    )
                ]

            backend = ScriptedBackend(
                steps,
                verdict_for=lambda _s, _sig: verdict,
                decompose_for=decompose,
            )
            client = self._app(backend, tmp)
            with client:
                r = client.post("/api/tasks", json={"goal": "做一个造文件的小工具"})
                self.assertEqual(r.status_code, 202, r.text)
                plan_id = r.json()["plan_id"]

                def plan_ready():
                    p = client.get(f"/api/plans/{plan_id}").json()
                    return p if p.get("status") == "ACCEPTED" else None

                plan = _wait_for(plan_ready, what="拆解完成")
                self.assertTrue(plan["dispatchable"])
                self.assertEqual(plan["root_id"], plan_id)
                # 本机 .env 有两家 key -> profiles 应该被惰性生成出来
                self.assertIn("available_providers", plan)

                r = client.post(f"/api/plans/{plan_id}/dispatch", json={})
                self.assertEqual(r.status_code, 202, r.text)
                root_id = r.json()["root_id"]

                def finished():
                    d = client.get(f"/api/tasks/{root_id}").json()
                    if d is None or d.get("kind") != "composite":
                        return None
                    tasks = d.get("tasks") or {}
                    if tasks and all(
                        t["status"] == "COMPLETED" for t in tasks.values()
                    ):
                        return d
                    return None

                detail = _wait_for(finished, what="复合任务跑完")
                self.assertIn("t1_build", detail["tasks"])
                self.assertEqual(detail["pending_children"], [])

                # 列表里复合任务折成一条
                threads = client.get("/api/tasks").json()
                row = next(t for t in threads if t["task_id"] == root_id)
                self.assertTrue(row["composite"])

    # ---------------------------------------------------------- #
    # 写端的错误面
    # ---------------------------------------------------------- #

    def test_intervene_and_ruling_errors(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            spec, backend = _restore_scenario(tmp / "ws")
            (tmp / "ws").mkdir()
            client = self._app(backend, tmp)
            with client:
                # 不存在的任务
                r = client.post(
                    "/api/tasks/task_nope/intervene", json={"instruction": "x"}
                )
                self.assertEqual(r.status_code, 409)
                r = client.post(
                    "/api/tasks/task_nope/ruling",
                    json={"action": "CONTINUE", "rationale": "x"},
                )
                self.assertEqual(r.status_code, 404)

                # 没到 AWAITING_HUMAN 的任务不能裁决
                orch = Orchestrator(
                    spec,
                    backend=backend,
                    store=client.app.state.runner.store,
                    human_gate=client.app.state.runner.gate,
                    log=QUIET,
                )
                orch.run()  # -> AWAITING_HUMAN
                # 坏 action 被 400 拦住
                r = client.post(
                    f"/api/tasks/{spec.id}/ruling",
                    json={"action": "YOLO", "rationale": "x"},
                )
                self.assertEqual(r.status_code, 400)

    # ---------------------------------------------------------- #
    # 设置页
    # ---------------------------------------------------------- #

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("# test env\n", encoding="utf-8")
            old_env_file = os.environ.get("COWORK_ENV_FILE")
            old_key = os.environ.get("DEEPSEEK_API_KEY")
            os.environ["COWORK_ENV_FILE"] = str(env_file)
            client = self._app(ScriptedBackend({}), Path(td))
            try:
                with client:
                    r = client.put(
                        "/api/providers/deepseek",
                        json={"api_key": "sk-test1234abcd"},
                    )
                    self.assertEqual(r.status_code, 200, r.text)

                    providers = client.get("/api/providers").json()
                    ds = next(p for p in providers if p["name"] == "deepseek")
                    self.assertTrue(ds["configured"])
                    self.assertEqual(ds["key_hint"], "····abcd")

                    # 值里带换行 = 往 .env 多写一行 = 任意环境变量注入。
                    # 一次「设置 key」的请求能顺手把 COWORK_LLM_BASE_URL 指到
                    # 别处，之后所有请求连同 key 一起送过去。必须 400 挡住。
                    bad = client.put(
                        "/api/providers/deepseek",
                        json={"api_key": "sk-x\nCOWORK_LLM_BASE_URL=http://坏人/"},
                    )
                    self.assertEqual(bad.status_code, 400, bad.text)
                    self.assertNotIn("坏人", env_file.read_text(encoding="utf-8"))
                    # 挡住之后原来的 key 也不该被改坏
                    self.assertIn("sk-test1234abcd", env_file.read_text(encoding="utf-8"))
                    # 完整 key 不出现在任何响应里 —— 只写不读
                    self.assertNotIn("sk-test1234abcd", client.get("/api/providers").text)
                    # 落盘了，且注释还在
                    text = env_file.read_text(encoding="utf-8")
                    self.assertIn("DEEPSEEK_API_KEY=sk-test1234abcd", text)
                    self.assertIn("# test env", text)

                    # 全局设置：挡位校验 + 回读
                    r = client.put(
                        "/api/settings", json={"effort": {"architect": "turbo"}}
                    )
                    self.assertEqual(r.status_code, 400)
                    r = client.put(
                        "/api/settings",
                        json={
                            "effort": {"architect": "max"},
                            "models": {"subagent": "deepseek-v4-pro"},
                        },
                    )
                    self.assertEqual(r.status_code, 200)
                    got = client.get("/api/settings").json()
                    self.assertEqual(got["effort"]["architect"], "max")
                    self.assertEqual(got["effort"]["subagent"], "medium")  # 默认没丢
                    self.assertEqual(got["models"]["subagent"], "deepseek-v4-pro")
            finally:
                if old_env_file is None:
                    os.environ.pop("COWORK_ENV_FILE", None)
                else:
                    os.environ["COWORK_ENV_FILE"] = old_env_file
                if old_key is None:
                    os.environ.pop("DEEPSEEK_API_KEY", None)
                else:
                    os.environ["DEEPSEEK_API_KEY"] = old_key

    # ---------------------------------------------------------- #
    # SSE：TestClient 关不掉无限流（已知怪癖），所以起真 uvicorn 测
    # ---------------------------------------------------------- #

    def test_sse_connect(self):
        import socket
        import threading
        import urllib.request

        import uvicorn

        with tempfile.TemporaryDirectory() as td:
            app = create_app(
                store=SqliteStore(":memory:"),
                backend_factory=lambda: ScriptedBackend({}),
                workspace=td,
            )
            # 先占一个空端口再放掉 —— 测试里够用的小竞态
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
            server = uvicorn.Server(config)
            threading.Thread(target=server.run, daemon=True).start()
            try:
                for _ in range(50):
                    if server.started:
                        break
                    time.sleep(0.1)
                self.assertTrue(server.started, "uvicorn 没起来")

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/stream", timeout=10
                ) as resp:
                    self.assertEqual(resp.status, 200)
                    first = resp.readline()
                    self.assertIn(b"retry", first)
            finally:
                server.should_exit = True


if __name__ == "__main__":
    unittest.main()
