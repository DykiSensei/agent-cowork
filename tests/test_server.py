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
from cowork.server import (  # 模块级不引 fastapi，只有调用时才需要
    check_bind_host,
    create_app,
    exposure_warning,
    is_loopback_host,
)
from cowork.store import SqliteStore
from cowork.types import (
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskState,
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

                # 人的原话立刻落在 root 线程上 —— 拆解还没跑完就该在了，
                # 否则 spec.goal 被架构师改写后就再也拿不回来（M6 §9）
                root_events = client.app.state.runner.store.events_for(plan_id)
                self.assertEqual(root_events[0].kind, "human")
                self.assertEqual(root_events[0].text, "做一个造文件的小工具")

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

                # 列表里复合任务折成一条，标题是人自己的话不是「复合任务（N）」
                threads = client.get("/api/tasks").json()
                row = next(t for t in threads if t["task_id"] == root_id)
                self.assertTrue(row["composite"])
                self.assertEqual(row["title"], "做一个造文件的小工具")
                # 详情里 root_goal 单独给一份（标题栏不该去翻时间线）
                self.assertEqual(detail["root_goal"], "做一个造文件的小工具")

    def test_plan_awaiting_human_then_ruling_then_dispatch(self):
        """界面上「发布任务」走的另一条分支：拆解没收敛 → 人拍板 → 派发。

        `test_plan_dispatch_flow` 覆盖的是复核一次通过、直接 ACCEPTED 那条。
        这条覆盖 AWAITING_HUMAN：**它不是错误**，是三种终局之一，界面要在这里
        给出「就按这份拆解跑 / 否决」两个按钮，点下去才是 ruling → dispatch。
        没有这条的话，界面上那两个按钮的语义没有任何东西钉着。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            def decompose(_goal, _feedback):
                return [
                    SubtaskDraft(
                        id="t1_build",
                        goal="造出 a.txt",
                        acceptance=[{"id": "c1", "description": "a.txt 被造出来"}],
                        scope=["a.txt"],
                    )
                ]

            backend = ScriptedBackend(
                {(1, 0): Finish(output={}, summary="done")},
                decompose_for=decompose,
                # 复核一直报缺口 -> 重生成用尽 -> 升级给人 -> ChatGate 立即返回 None
                review_for=lambda *_: (False, ["原始目标里的「一页」没有验收标准管它"]),
            )
            client = self._app(backend, tmp)
            with client:
                r = client.post("/api/tasks", json={"goal": "做一个造文件的小工具"})
                plan_id = r.json()["plan_id"]

                def settled():
                    p = client.get(f"/api/plans/{plan_id}").json()
                    return p if p.get("status") not in (None, "RUNNING") else None

                plan = _wait_for(settled, what="拆解收敛")
                self.assertEqual(plan["status"], "AWAITING_HUMAN")
                self.assertFalse(plan["dispatchable"], "没人拍板之前不能派发")
                self.assertTrue(plan["specs"], "要有一份拆解摆给人看，否则没什么可裁决的")
                self.assertTrue(plan["escalation_reason"])

                # 没拍板就派发 -> 409（界面上那个按钮此时也不该出现）
                self.assertEqual(
                    client.post(f"/api/plans/{plan_id}/dispatch", json={}).status_code, 409
                )

                # 「就按这份拆解跑」
                r = client.post(
                    f"/api/plans/{plan_id}/ruling",
                    json={"accept": True, "rationale": "人确认按当前拆解执行"},
                )
                self.assertEqual(r.status_code, 200, r.text)
                self.assertTrue(client.get(f"/api/plans/{plan_id}").json()["dispatchable"])

                r = client.post(f"/api/plans/{plan_id}/dispatch", json={})
                self.assertEqual(r.status_code, 202, r.text)
                self.assertEqual(r.json()["root_id"], plan_id)

                # **派发是 202，活儿在后台线程里。** 不等它收尾就退出 with 块的话，
                # `TemporaryDirectory` 会在子任务还在往里写文件时删目录 ——
                # 症状是偶发的 `WinError 145 目录不是空的`，和被测行为无关。
                _wait_for(
                    lambda: all(
                        p["terminal"]
                        for p in client.get(f"/api/tasks/{plan_id}")
                        .json()["progress"]
                        .values()
                    )
                    or None,
                    what="派发出去的子任务收尾",
                )

    def test_an_unpatched_plan_is_takeoverable_from_the_detail(self):
        """M12 待办 #1：拆解中 / 等拍板 / 等派发的 plan 要能在详情页接管。

        发布框一关，plan_id 就丢在组件状态里了；服务端其实一直留着 plan。
        钉住两件事：列表把「等拍板」标成 AWAITING_HUMAN（红点亮、排前面），
        详情带上 plan_entry（界面据此给裁决 / 派发入口），派发之后它消失。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            def decompose(_goal, _feedback):
                return [
                    SubtaskDraft(
                        id="t1_build",
                        goal="造出 a.txt",
                        acceptance=[{"id": "c1", "description": "a.txt 被造出来"}],
                        scope=["a.txt"],
                    )
                ]

            backend = ScriptedBackend(
                {(1, 0): Finish(output={}, summary="done")},
                decompose_for=decompose,
                review_for=lambda *_: (False, ["原始目标里的「一页」没有验收标准管它"]),
            )
            client = self._app(backend, tmp)
            with client:
                plan_id = client.post(
                    "/api/tasks", json={"goal": "做一个造文件的小工具"}
                ).json()["plan_id"]

                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("status")
                    == "AWAITING_HUMAN",
                    what="拆解升级给人",
                )

                # 列表：等拍板要标 AWAITING_HUMAN，而不是 PENDING「排队中」
                row = next(
                    t for t in client.get("/api/tasks").json()
                    if t["task_id"] == plan_id
                )
                self.assertEqual(row["status"], "AWAITING_HUMAN")

                # 详情：带上 plan_entry，界面据此给「去裁决」入口
                d = client.get(f"/api/tasks/{plan_id}").json()
                self.assertEqual(d["kind"], "composite")
                self.assertIn("plan_entry", d)
                self.assertEqual(d["plan_entry"]["status"], "AWAITING_HUMAN")
                self.assertFalse(d["plan_entry"]["dispatchable"])

                # 人拍板「就按这份跑」之后：等派发，plan_entry 跟着变
                r = client.post(
                    f"/api/plans/{plan_id}/ruling",
                    json={"accept": True, "rationale": "人确认按当前拆解执行"},
                )
                self.assertEqual(r.status_code, 200, r.text)
                d = client.get(f"/api/tasks/{plan_id}").json()
                self.assertTrue(d["plan_entry"]["dispatchable"])

                # 派发之后 plan_entry 消失（线程变成正常的复合线程）
                r = client.post(f"/api/plans/{plan_id}/dispatch", json={})
                self.assertEqual(r.status_code, 202, r.text)
                d = client.get(f"/api/tasks/{plan_id}").json()
                self.assertNotIn("plan_entry", d)

                # 别让 TemporaryDirectory 在子任务还写文件时被删（同别处）
                _wait_for(
                    lambda: all(
                        p["terminal"]
                        for p in client.get(f"/api/tasks/{plan_id}")
                        .json()["progress"].values()
                    )
                    or None,
                    what="派发出去的子任务收尾",
                )

    def test_the_journey_a_human_actually_takes(self):
        """实测走过的那条路，逐跳钉住 —— 它踩到了两个界面上无解的坑。

        发布 → 拆解 → 派发 → 子任务挂起 → 在复合线程上答复。

        坑一：**派发成功的那一刻，root 线程还没有任何 tasks 行**（子任务要等各自
        的 Orchestrator 起跑）。详情回 404，而界面正好在这一刻切过去 ——
        整页变成「连不上服务」，刷新一下又好了。线程的存在性看事件，不看 tasks 行。

        坑二：**子任务折在父线程里，侧栏点不到**。它挂起时复合详情只给了一串
        `pending_children` 的 id，拿不到升级原因和系统建议 —— 于是渲染不出裁决
        表单，任务停着而人无处答复。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            def decompose(_goal, _feedback):
                return [
                    SubtaskDraft(
                        id="t1_build",
                        goal="造出 a.txt",
                        acceptance=[{"id": "c1", "description": "a.txt 被造出来",
                                     "command": ["python", "-c", "import sys;sys.exit(1)"]}],
                        scope=["a.txt"],
                    )
                ]

            backend = ScriptedBackend(
                {(1, 0): Finish(output={}, summary="做完了")},
                decompose_for=decompose,
                # 验收命令必然失败 -> TEST_FAILED -> 裁决 ABANDON -> 必然升级
                # -> ChatGate 立即返回 None -> 子任务 AWAITING_HUMAN
                verdict_for=lambda *_: ArchitectVerdict(
                    action="ABANDON", rationale="做不下去了", complexity_score=0.9
                ),
            )
            client = self._app(backend, tmp)
            with client:
                plan_id = client.post(
                    "/api/tasks", json={"goal": "做一个造文件的小工具"}
                ).json()["plan_id"]

                # 坑一的前半：拆解还在跑，线程就该看得见了（人的原话已经落库）
                r = client.get(f"/api/tasks/{plan_id}")
                self.assertEqual(r.status_code, 200, "有事件就是有线程，不能 404")
                self.assertEqual(r.json()["root_goal"], "做一个造文件的小工具")
                self.assertIn(
                    plan_id, [t["task_id"] for t in client.get("/api/tasks").json()],
                    "刚发布的任务必须出现在列表里，否则人以为没发出去",
                )

                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("status")
                    == "ACCEPTED",
                    what="拆解完成",
                )
                root_id = client.post(
                    f"/api/plans/{plan_id}/dispatch", json={}
                ).json()["root_id"]

                # 坑一的后半：派发成功的这一刻立刻拉详情（界面就是这么干的）
                r = client.get(f"/api/tasks/{root_id}")
                self.assertEqual(r.status_code, 200, "派发瞬间的真空期不能回 404")

                # 坑二：子任务挂起之后，复合详情要给得出裁决材料
                def child_waiting():
                    d = client.get(f"/api/tasks/{root_id}").json()
                    return d if d.get("pending_children") else None

                detail = _wait_for(child_waiting, what="子任务挂起")
                child = detail["pending_children"][0]
                self.assertIsNotNone(
                    detail["pending"][child],
                    "只有一串 id 的话，界面渲染不出表单，人无处答复",
                )
                self.assertIn("ABANDON", detail["pending"][child]["reason"])
                self.assertIn(child, detail["progress"], "还要看得出它跑到哪了")

                # 在复合线程上答复，裁决发给**子任务**
                r = client.post(
                    f"/api/tasks/{child}/ruling",
                    json={"action": "ABANDON", "rationale": "确实做不下去"},
                )
                self.assertEqual(r.status_code, 202, r.text)

    def test_plan_rejection_stays_undispatchable(self):
        """「否决」之后不能再派发 —— 否则那个按钮就是个摆设。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            backend = ScriptedBackend(
                {},
                decompose_for=lambda _g, _f: [
                    SubtaskDraft(
                        id="t1", goal="g",
                        acceptance=[{"id": "c1", "description": "d"}], scope=["a.txt"],
                    )
                ],
                review_for=lambda *_: (False, ["缺口"]),
            )
            client = self._app(backend, tmp)
            with client:
                plan_id = client.post("/api/tasks", json={"goal": "g"}).json()["plan_id"]
                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("status")
                    == "AWAITING_HUMAN",
                    what="拆解挂起",
                )
                client.post(f"/api/plans/{plan_id}/ruling", json={"accept": False})
                self.assertEqual(
                    client.post(f"/api/plans/{plan_id}/dispatch", json={}).status_code, 409
                )

    # ---------------------------------------------------------- #
    # 工作区与「接手已有项目」（§12 M10）
    # ---------------------------------------------------------- #

    def _plan_backend(self):
        return ScriptedBackend(
            {},
            decompose_for=lambda _g, _f: [
                SubtaskDraft(
                    id="t1", goal="改一改",
                    acceptance=[{"id": "c1", "description": "改好了"}],
                    scope=["app.py"],
                )
            ],
        )

    def test_new_task_gets_its_own_subdirectory(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._app(self._plan_backend(), Path(td))
            with client:
                plan_id = client.post(
                    "/api/tasks", json={"goal": "做点东西", "workspace": td}
                ).json()["plan_id"]
                plan = _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json()
                    if client.get(f"/api/plans/{plan_id}").json().get("workspace")
                    else None,
                    what="拆解开始",
                )
                self.assertEqual(plan["workspace"], str(Path(td) / plan_id))
                self.assertFalse(plan["takeover"])

    def test_takeover_writes_into_the_directory_itself(self):
        """接手时落进子目录的话，改的就不是人手上那份代码，而是它的拷贝。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "app.py").write_text("print(1)", encoding="utf-8")
            client = self._app(self._plan_backend(), Path(td))
            with client:
                plan_id = client.post(
                    "/api/tasks",
                    json={"goal": "把 app.py 改好", "workspace": td, "mode": "takeover"},
                ).json()["plan_id"]
                plan = _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json()
                    if client.get(f"/api/plans/{plan_id}").json().get("status")
                    != "RUNNING"
                    else None,
                    what="拆解完成",
                )
                self.assertEqual(plan["workspace"], str(Path(td)))
                self.assertTrue(plan["takeover"])

    def test_takeover_shows_the_architect_what_is_already_there(self):
        """**这就是「半路接手」和「从零开始」的全部区别。**

        不把现状给生成者，它会把一个有内容的目录当空目录，从零重建一遍。
        """
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "app.py").write_text("print(1)", encoding="utf-8")
            (Path(td) / "README.md").write_text("# 项目", encoding="utf-8")
            backend = self._plan_backend()
            client = self._app(backend, Path(td))
            with client:
                plan_id = client.post(
                    "/api/tasks",
                    json={"goal": "加一个 CLI", "workspace": td, "mode": "takeover"},
                ).json()["plan_id"]
                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("status")
                    != "RUNNING",
                    what="拆解完成",
                )

            existing = backend.decompose_existing[0]
            self.assertIsNotNone(existing, "接手模式必须把现状送到生成者手上")
            self.assertIn("app.py", existing)
            self.assertIn("README.md", existing)
            self.assertIn("不是一个空目录", existing)

    def test_a_fresh_start_does_not_pay_for_a_snapshot(self):
        """从零开始的目录本来就是空的，采集它只是白花提示词。"""
        with tempfile.TemporaryDirectory() as td:
            backend = self._plan_backend()
            client = self._app(backend, Path(td))
            with client:
                plan_id = client.post(
                    "/api/tasks", json={"goal": "做点东西", "workspace": td}
                ).json()["plan_id"]
                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("status")
                    != "RUNNING",
                    what="拆解完成",
                )
            self.assertIsNone(backend.decompose_existing[0])

    def test_a_bad_workspace_is_refused_before_anything_starts(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._app(self._plan_backend(), Path(td))
            with client:
                for bad in ("./out", str(Path(td) / "nope" / "deeper")):
                    r = client.post(
                        "/api/tasks", json={"goal": "x", "workspace": bad}
                    )
                    self.assertEqual(r.status_code, 400, f"{bad}: {r.text}")
                r = client.post("/api/tasks", json={"goal": "x", "mode": "yolo"})
                self.assertEqual(r.status_code, 400)

    def test_delete_thread_removes_records_but_not_files(self):
        """**删记录不删文件。** 产物是人的东西 —— 删一条聊天记录不该顺手删他的代码。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            spec, backend = _restore_scenario(tmp / "ws")
            (tmp / "ws").mkdir()
            (tmp / "ws" / "keep.txt").write_text("人的东西", encoding="utf-8")
            client = self._app(backend, tmp)
            with client:
                runner = client.app.state.runner
                Orchestrator(spec, backend=backend, store=runner.store,
                             human_gate=runner.gate, log=QUIET).run()
                self.assertIsNotNone(runner.store.load_task(spec.id))

                r = client.delete(f"/api/tasks/{spec.id}")
                self.assertEqual(r.status_code, 200, r.text)

                self.assertIsNone(runner.store.load_task(spec.id))
                self.assertEqual(runner.store.events_for(spec.id), [])
                self.assertEqual(runner.store.signals_for(spec.id), [])
                self.assertEqual(runner.store.decisions_for(spec.id), [])
                self.assertNotIn(
                    spec.id, [t["task_id"] for t in client.get("/api/tasks").json()]
                )
                self.assertTrue((tmp / "ws" / "keep.txt").exists(),
                                "工作区里的文件不能被删")

    def test_a_running_task_cannot_be_deleted(self):
        """边跑边删等于没删：后面每一次 save_task 都会把行写回来。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            spec, backend = _restore_scenario(tmp / "ws")
            (tmp / "ws").mkdir()
            client = self._app(backend, tmp)
            with client:
                runner = client.app.state.runner
                runner.running[spec.id] = Orchestrator(
                    spec, backend=backend, store=runner.store,
                    human_gate=runner.gate, log=QUIET,
                )
                try:
                    r = client.delete(f"/api/tasks/{spec.id}")
                    self.assertEqual(r.status_code, 409)
                    self.assertIn("先点", r.json()["error"])
                finally:
                    runner.running.pop(spec.id, None)

    def test_folder_picker_lists_directories(self):
        """浏览器拿不到本机绝对路径，只能让服务端列 —— 这是路径能点选的前提。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "proj").mkdir()
            (tmp / "proj" / "sub").mkdir()
            (tmp / "proj" / "a.txt").write_text("x", encoding="utf-8")
            client = self._app(self._plan_backend(), tmp)
            with client:
                # 不给 path 时给几个起点，而不是从文件系统根开始
                roots = client.get("/api/fs").json()
                self.assertTrue(roots["roots"])
                self.assertTrue(roots["entries"])

                got = client.get("/api/fs", params={"path": str(tmp / "proj")}).json()
                names = [e["name"] for e in got["entries"]]
                self.assertEqual(names, ["sub"], "只列子目录，文件是噪声")
                self.assertEqual(got["parent"], str(tmp))

                r = client.get("/api/fs", params={"path": str(tmp / "nope")})
                self.assertEqual(r.status_code, 400)

    def test_planning_progress_lands_in_the_timeline(self):
        """拆解过程要落成事件，不能只是一条 SSE 广播。

        广播没人订阅就消失，刷新页面也拿不回来 —— 于是「架构师在干什么」
        在界面上是一段空白（实测原话：不知道它在干啥，卡住了也不知道）。
        """
        with tempfile.TemporaryDirectory() as td:
            client = self._app(self._plan_backend(), Path(td))
            with client:
                plan_id = client.post(
                    "/api/tasks", json={"goal": "做点东西", "workspace": td}
                ).json()["plan_id"]
                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("status")
                    != "RUNNING",
                    what="拆解完成",
                )
                texts = [
                    e["text"]
                    for e in client.get(f"/api/tasks/{plan_id}").json()["events"]
                    if e["kind"] == "log"
                ]
                self.assertTrue(any("开始拆解" in t for t in texts))
                self.assertTrue(any("产物会落在" in t for t in texts))
                self.assertTrue(any("终局" in t for t in texts))

    def test_settings_carry_the_role_providers_and_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._app(self._plan_backend(), Path(td))
            with client:
                got = client.get("/api/settings").json()
                self.assertIn("providers", got)
                self.assertEqual(
                    sorted(got["providers"]), ["architect", "reviewer", "subagent"]
                )
                self.assertTrue(
                    got["workspace_default"], "没配工作区时也要说得出东西落在哪"
                )

                # 只能选已经配了 key 的家 —— 选一家没 key 的会在起跑时才失败，
                # 而那时人已经离开设置页了
                r = client.put(
                    "/api/settings", json={"providers": {"architect": "没这家"}}
                )
                self.assertEqual(r.status_code, 400)
                self.assertIn("还没配 API key", r.json()["error"])

                # 复核者多一个 none：明确关掉独立复核，退回同模型复核
                r = client.put("/api/settings", json={"providers": {"reviewer": "none"}})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(
                    client.get("/api/settings").json()["providers"]["reviewer"], "none"
                )

                r = client.put("/api/settings", json={"workspace": "./相对路径"})
                self.assertEqual(r.status_code, 400)
                self.assertIn("绝对路径", r.json()["error"])

    def _finished_task(self, tmp: Path):
        """跑出一个 COMPLETED 的单任务，返回 (client, spec, backend)。"""
        spec = TaskSpec(
            goal="写一个 README",
            parent_id="task_parent",  # 避开 §7.2 的顶层保护
            acceptance=[Criterion(id="c1", description="有文件")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(tmp)),
            scope=["README.md"],
            tools=["write_file"],
            max_steps=8,
        )
        backend = ScriptedBackend(
            {
                (1, 0): ToolCall("write_file", {"path": "README.md", "content": "# 标题\n"}),
                (1, 1): Finish(output={}),
                (2, 0): ToolCall(
                    "write_file", {"path": "README.md", "content": "# 标题\n## 用法\n"}
                ),
                (2, 1): Finish(output={}),
            }
        )
        client = self._app(backend, tmp)
        return client, spec, backend

    def test_follow_up_restarts_a_finished_task(self):
        """终局之后还能改（M12）。

        实测反馈：「当前任务完成之后就无法进行修改，很多时候一次不能做出符合
        要求的产出」。原来 intervene 回 409（不在活任务表里）、ruling 回 409
        （不是 AWAITING_HUMAN）—— 人手上一条路都没有。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client, spec, backend = self._finished_task(tmp)
            with client:
                runner = client.app.state.runner
                orch = Orchestrator(
                    spec, backend=backend, store=runner.store,
                    human_gate=runner.gate, log=QUIET,
                )
                self.assertIs(orch.run().state.status, TaskStatus.COMPLETED)

                r = client.post(
                    f"/api/tasks/{spec.id}/follow-up",
                    json={"instruction": "再补一节「用法」"},
                )
                self.assertEqual(r.status_code, 202, r.text)
                self.assertEqual(r.json()["kind"], "single")

                def done():
                    d = client.get(f"/api/tasks/{spec.id}").json()
                    return d if d["state"]["spec"]["revision"] == 2 else None

                final = _wait_for(done, what="续跑跑到第 2 版")
                self.assertEqual(final["state"]["status"], "COMPLETED")
                self.assertIn(
                    "用法", (tmp / "README.md").read_text(encoding="utf-8")
                )
                # 人说的那句话要在时间线上
                self.assertIn(
                    "再补一节「用法」",
                    [e["text"] for e in final["events"] if e["kind"] == "human"],
                )

    def test_follow_up_on_a_running_task_is_refused_with_a_reason(self):
        """还在跑的时候「追加」就是介入 —— 拒绝要说清该用哪条路。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client, spec, backend = self._finished_task(tmp)
            with client:
                runner = client.app.state.runner
                orch = Orchestrator(
                    spec, backend=backend, store=runner.store, log=QUIET
                )
                orch.run()
                runner.running[spec.id] = orch  # 假装它还在跑

                r = client.post(
                    f"/api/tasks/{spec.id}/follow-up", json={"instruction": "改一下"}
                )
                self.assertEqual(r.status_code, 409)
                self.assertIn("介入", r.json()["error"])

    def test_follow_up_on_a_composite_thread_replans_in_place(self):
        """复合线程的追加要求 = 在同一条线程上、带着现状再拆一轮（M12）。

        **不另起一条线程**：注册表本来就按 root 键，新一轮直接顶掉旧条目。
        逐个子任务续跑做不到这件事 —— 追加要求落在哪个子任务上是个判断题，
        而那正是拆解要做的事。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._app(self._plan_backend(), tmp)
            with client:
                runner = client.app.state.runner
                plan_id = client.post(
                    "/api/tasks", json={"goal": "做点东西", "workspace": td}
                ).json()["plan_id"]
                _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json().get("workspace"),
                    what="拆解开始",
                )
                ws = client.get(f"/api/plans/{plan_id}").json()["workspace"]

                r = client.post(
                    f"/api/tasks/{plan_id}/follow-up",
                    json={"instruction": "再加一个导出功能"},
                )
                self.assertEqual(r.status_code, 202, r.text)
                self.assertEqual(r.json()["kind"], "composite")

                again = _wait_for(
                    lambda: client.get(f"/api/plans/{plan_id}").json()
                    if client.get(f"/api/plans/{plan_id}").json().get("goal")
                    == "再加一个导出功能"
                    else None,
                    what="新一轮拆解接管同一条线程",
                )
                self.assertEqual(again["workspace"], ws, "要在原来的产物上接着做")
                self.assertTrue(again["takeover"], "追加一轮就是「接手已有项目」")
                self.assertIn(
                    "再加一个导出功能",
                    [
                        e["text"]
                        for e in client.get(f"/api/tasks/{plan_id}").json()["events"]
                        if e["kind"] == "human"
                    ],
                    "人说的话要留在这条线程上",
                )
                self.assertIsNotNone(runner.plans[plan_id])

    def test_follow_up_finds_the_workspace_after_a_restart(self):
        """进程重启之后 `plans` 是空的，而线程还在库里。

        那时候「产物在哪」的唯一答案是子任务的 `spec.sandbox.workspace` ——
        只信内存的话，重启过一次的线程就再也追加不了了。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._app(self._plan_backend(), tmp)
            with client:
                runner = client.app.state.runner
                kid = TaskSpec(
                    id="t_kid", parent_id="root_x", goal="干活",
                    acceptance=[Criterion(id="c1", description="做完")],
                    task_class=TaskClass.CODE,
                    sandbox=SandboxProfile(workspace=str(tmp / "产物")),
                    scope=["a.py"],
                )
                runner.store.save_task(
                    TaskState(spec=kid, status=TaskStatus.COMPLETED)
                )
                self.assertNotIn("root_x", runner.plans, "模拟重启：注册表里没有它")

                r = client.post(
                    "/api/tasks/root_x/follow-up", json={"instruction": "再改改"}
                )
                self.assertEqual(r.status_code, 202, r.text)
                self.assertEqual(runner.plans["root_x"].workspace, str(tmp / "产物"))

    def test_follow_up_waits_for_the_children_to_stop(self):
        """还有子任务在跑的时候不能重拆 —— 那等于一边跑一边换规格。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._app(self._plan_backend(), tmp)
            with client:
                runner = client.app.state.runner
                kid = TaskSpec(
                    id="t_kid2", parent_id="root_y", goal="干活",
                    acceptance=[Criterion(id="c1", description="做完")],
                    task_class=TaskClass.CODE,
                    sandbox=SandboxProfile(workspace=str(tmp)),
                    scope=["a.py"],
                )
                runner.store.save_task(
                    TaskState(spec=kid, status=TaskStatus.RUNNING)
                )

                r = client.post(
                    "/api/tasks/root_y/follow-up", json={"instruction": "再改改"}
                )
                self.assertEqual(r.status_code, 409)
                self.assertIn("收尾", r.json()["error"])

    def test_skills_are_listed_and_ride_into_the_specs(self):
        """人勾的说明书要一路走到子任务的 spec 上（M12）。

        清单里还要带上**目录在哪** —— 列表为空时人得知道该把文件放哪，
        否则只能猜（同工作区那条：默认值必须是人找得到的地方）。
        """
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sk = tmp / "skills"
            (sk / "py-style").mkdir(parents=True)
            (sk / "py-style" / "SKILL.md").write_text(
                "---\nname: py-style\ndescription: 风格约定\n---\n缩进四个空格",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"COWORK_SKILLS_DIR": str(sk)}):
                client = self._app(self._plan_backend(), tmp)
                with client:
                    runner = client.app.state.runner
                    got = client.get("/api/skills").json()
                    self.assertEqual(got["root"], str(sk))
                    self.assertEqual(
                        [s["name"] for s in got["skills"]], ["py-style"]
                    )
                    self.assertNotIn("body", got["skills"][0], "列表不驮正文")

                    plan_id = client.post(
                        "/api/tasks",
                        json={
                            "goal": "做点东西",
                            "workspace": str(tmp),
                            # 不存在的那个要被静默筛掉，不能带进 spec
                            "skills": ["py-style", "并不存在"],
                        },
                    ).json()["plan_id"]
                    plan = _wait_for(
                        lambda: client.get(f"/api/plans/{plan_id}").json()
                        if client.get(f"/api/plans/{plan_id}").json().get("specs")
                        else None,
                        what="拆解出子任务",
                    )
                    self.assertEqual(plan["specs"][0]["skills"], ["py-style"])
                    self.assertEqual(runner.plans[plan_id].skills, ["py-style"])

    def test_skills_must_be_a_list_of_strings(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._app(self._plan_backend(), Path(td))
            with client:
                r = client.post(
                    "/api/tasks", json={"goal": "做点东西", "skills": "py-style"}
                )
                self.assertEqual(r.status_code, 400)

    def test_follow_up_needs_something_to_say(self):
        with tempfile.TemporaryDirectory() as td:
            client, spec, _ = self._finished_task(Path(td))
            with client:
                r = client.post(
                    f"/api/tasks/{spec.id}/follow-up", json={"instruction": "  "}
                )
                self.assertEqual(r.status_code, 400)

    def test_max_parallel_is_the_humans_to_set(self):
        """并发度归人（M12）。

        以前 `dispatch()` 压根不传这个参数，吃 Scheduler 的默认 4 —— 一层里
        第 5 个任务开始排队，而排队在界面上和「卡住」长得一模一样。
        `0` 是「有几个跑几个」，不是「一个都不跑」。
        """
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            client = self._app(self._plan_backend(), Path(td))
            with client:
                runner = client.app.state.runner
                self.assertIn("max_parallel", client.get("/api/settings").json())

                with mock.patch.dict(os.environ, {"COWORK_MAX_PARALLEL": ""}):
                    self.assertEqual(runner._max_parallel(9), 4)
                with mock.patch.dict(os.environ, {"COWORK_MAX_PARALLEL": "2"}):
                    self.assertEqual(runner._max_parallel(9), 2)
                with mock.patch.dict(os.environ, {"COWORK_MAX_PARALLEL": "0"}):
                    self.assertEqual(runner._max_parallel(9), 9, "0 = 不额外限制")
                with mock.patch.dict(os.environ, {"COWORK_MAX_PARALLEL": "什么"}):
                    self.assertEqual(runner._max_parallel(9), 4, "填坏了要回落，不能崩")

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

                # 畸形 / 空 body 是 400（缺什么说什么），不是 500
                for kw in ({}, {"content": b"not json"}, {"json": {}}):
                    r = client.post(f"/api/tasks/{spec.id}/ruling", **kw)
                    self.assertEqual(r.status_code, 400, r.text)
                r = client.post("/api/tasks", content=b"{oops")
                self.assertEqual(r.status_code, 400, r.text)

                # 拆解裁决尤其不能把「字段缺失」落到 accept=False 上 ——
                # 否决是有后果的，不该由一次畸形请求代人做出
                r = client.post("/api/plans/plan_nope/ruling", json={})
                self.assertEqual(r.status_code, 400, r.text)

    def test_cancel_endpoint(self):
        """取消的**接线**（端点 → runner → orchestrator.cancel）。

        语义（不问架构师、停在 step 边界、产出保留）在 `test_cancel.py` 里钉，
        那边不需要起 HTTP 也就不会有时序抖动。这里只验证注册表这一跳。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            spec, backend = _restore_scenario(tmp / "ws")
            (tmp / "ws").mkdir()
            client = self._app(backend, tmp)
            with client:
                runner = client.app.state.runner

                # 不在运行中 -> 409，且提示要告诉用户「那该怎么办」。
                # **判据是用户看得懂**：原来这句话写的是「请用 ruling
                # （action=ABANDON）」—— ruling / step 边界这些是系统内部的说法，
                # 用户没有理由知道它们（实测反馈就卡在这句话上）。
                r = client.post("/api/tasks/task_nope/cancel", json={})
                self.assertEqual(r.status_code, 409)
                msg = r.json()["error"]
                self.assertIn("放弃", msg, "要指出「在卡片里选放弃」这条路")
                for jargon in ("ruling", "step", "AWAITING_HUMAN"):
                    self.assertNotIn(jargon, msg, f"给用户看的话里不该有 {jargon}")

                # **body 是可选的**（契约写的是 `{reason?}`）。原来的判据是
                # `req.headers.get("content-length")` —— 那是字符串，"0" 为真，
                # 于是 `curl -X POST` 这种带 Content-Length: 0 的请求会去解析
                # 空 body，抛出去就是 500。可选就得真的可选。
                for label, kw in (
                    ("完全没有 body", {}),
                    ("Content-Length: 0", {"headers": {"content-length": "0"},
                                           "content": b""}),
                ):
                    r = client.post("/api/tasks/task_nope/cancel", **kw)
                    self.assertEqual(r.status_code, 409, f"{label}: {r.text}")

                orch = Orchestrator(
                    spec,
                    backend=backend,
                    store=runner.store,
                    human_gate=runner.gate,
                    log=QUIET,
                )
                runner.running[spec.id] = orch  # runner 起跑时就是这么登记的
                try:
                    r = client.post(
                        f"/api/tasks/{spec.id}/cancel", json={"reason": "不做了"}
                    )
                    self.assertEqual(r.status_code, 202, r.text)
                    self.assertIsNotNone(orch._cancelled)
                    self.assertIn("不做了", orch._cancelled)
                    kinds = [e.kind for e in runner.store.events_for(spec.id)]
                    self.assertIn("human", kinds)
                finally:
                    runner.running.pop(spec.id, None)

    def test_review_writes_setting_roundtrip(self):
        """写入侧复核的开关走设置页。

        **必须收字符串 on/off，不能收布尔**：它落到 .env，而空串在那里是
        「未设置」→ 回落到默认（on）。前端发 false 的话反而关不掉。
        """
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("# test env\n", encoding="utf-8")
            old = os.environ.get("COWORK_ENV_FILE")
            old_flag = os.environ.get("COWORK_REVIEW_WRITES")
            os.environ["COWORK_ENV_FILE"] = str(env_file)
            os.environ.pop("COWORK_REVIEW_WRITES", None)
            client = self._app(ScriptedBackend({}), Path(td))
            try:
                with client:
                    self.assertEqual(
                        client.get("/api/settings").json()["review_writes"], "on",
                        "默认开（§11.19）",
                    )

                    r = client.put("/api/settings", json={"review_writes": "off"})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertIn("COWORK_REVIEW_WRITES=off", env_file.read_text("utf-8"))

                    # 布尔会被拒 —— 不然 str(False or "") 是空串，等于没关
                    r = client.put("/api/settings", json={"review_writes": False})
                    self.assertEqual(r.status_code, 400)
                    r = client.put("/api/settings", json={"review_writes": "yes"})
                    self.assertEqual(r.status_code, 400)
            finally:
                for k, v in (("COWORK_ENV_FILE", old), ("COWORK_REVIEW_WRITES", old_flag)):
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_search_settings_roundtrip_and_key_discipline(self):
        """联网搜索走设置页：供应商可读写，**key 只写不读**。

        两把 key 的关系也要能从 GET 看出来（`key_source`）：专用 key 优先，
        没有就用那家自己的。少了这个，界面只能显示一个没有下文的「已配置」，
        人不知道自己配的是哪一把、也不知道该去哪儿改。
        """
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("# test env\n", encoding="utf-8")
            saved = {
                k: os.environ.get(k)
                for k in ("COWORK_ENV_FILE", "COWORK_SEARCH_API_KEY",
                          "COWORK_SEARCH_PROVIDER", "ZHIPUAI_API_KEY")
            }
            os.environ["COWORK_ENV_FILE"] = str(env_file)
            for k in ("COWORK_SEARCH_API_KEY", "COWORK_SEARCH_PROVIDER",
                      "ZHIPUAI_API_KEY"):
                os.environ.pop(k, None)
            client = self._app(ScriptedBackend({}), Path(td))
            try:
                with client:
                    got = client.get("/api/settings").json()["search"]
                    self.assertFalse(got["configured"], "一把 key 都没有")
                    self.assertIsNone(got["key_source"])
                    self.assertEqual(got["effective_provider"], "zhipu")
                    self.assertEqual(got["provider_key_env"], "ZHIPUAI_API_KEY",
                                     "界面要能说出「配哪个变量」")

                    # 那家自己的 key 就够用 —— 配过智谱的人不用再填任何东西
                    os.environ["ZHIPUAI_API_KEY"] = "zp-abcd1234"
                    got = client.get("/api/settings").json()["search"]
                    self.assertTrue(got["configured"])
                    self.assertEqual(got["key_source"], "provider")

                    # 专用 key 优先
                    r = client.put("/api/search/key", json={"api_key": "sk-search-9999"})
                    self.assertEqual(r.status_code, 200, r.text)
                    got = client.get("/api/settings").json()["search"]
                    self.assertEqual(got["key_source"], "dedicated")
                    self.assertEqual(got["key_hint"], "····9999")

                    # **完整 key 不出服务端**：整份响应里不该出现它
                    self.assertNotIn("sk-search-9999",
                                     client.get("/api/settings").text)

                    # 清掉专用 key = 回落到那家自己的
                    r = client.put("/api/search/key", json={"api_key": ""})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(
                        client.get("/api/settings").json()["search"]["key_source"],
                        "provider",
                    )

                    # 换行 = .env 注入，必须 400 而不是 500（同供应商 key 那条）
                    r = client.put(
                        "/api/search/key",
                        json={"api_key": "x\nCOWORK_LLM_BASE_URL=http://坏人/"},
                    )
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertNotIn("坏人", env_file.read_text("utf-8"))

                    # 供应商可写，但不认识的当场拒
                    r = client.put("/api/settings",
                                   json={"search": {"provider": "没这家"}})
                    self.assertEqual(r.status_code, 400, r.text)
                    r = client.put("/api/settings",
                                   json={"search": {"provider": "zhipu"}})
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertIn("COWORK_SEARCH_PROVIDER=zhipu",
                                  env_file.read_text("utf-8"))
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_search_not_configured_changes_nothing_else(self):
        """**不配搜索不影响主体功能** —— 这是这个功能的前提，得有东西钉着。

        没配 key 时：设置页照常打开、任务照常发布、工具面只是少一个
        `search_web`（连 `fetch_url` 都不受影响）。
        """
        with tempfile.TemporaryDirectory() as td:
            saved = {
                k: os.environ.get(k)
                for k in ("COWORK_SEARCH_API_KEY", "ZHIPUAI_API_KEY",
                          "COWORK_ALLOW_NETWORK")
            }
            for k in ("COWORK_SEARCH_API_KEY", "ZHIPUAI_API_KEY"):
                os.environ.pop(k, None)
            os.environ["COWORK_ALLOW_NETWORK"] = "on"
            client = self._app(ScriptedBackend({}), Path(td))
            try:
                with client:
                    self.assertEqual(client.get("/api/settings").status_code, 200)
                    self.assertEqual(client.get("/api/tasks").status_code, 200)
                    self.assertEqual(client.get("/api/providers").status_code, 200)

                    tools = client.app.state.runner._network_tools()
                    self.assertIn("fetch_url", tools, "抓网页不该受搜索连累")
                    self.assertNotIn("search_web", tools,
                                     "没 key 就别放进白名单：调了必然失败，白费一步")
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_provider_test_endpoint(self):
        """「已填」和「能用」是两件事。

        设置页原来只显示「已配置」，判据是环境变量非空 —— 填错一个 key 照样
        显示已配置、任务照样 401。这个端点让人能当场问一句「现在能用吗」。
        """
        with tempfile.TemporaryDirectory() as td:
            client = self._app(ScriptedBackend({}), Path(td))
            with client:
                r = client.post("/api/providers/没这家/test")
                self.assertEqual(r.status_code, 404)

                r = client.post("/api/providers/anthropic/test")
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                # anthropic 走自己的 SDK，不吃 /v1/models —— 结论是「没测到」，
                # **不是「失败」**。四个状态的结论不同，不能混成一个 bool
                self.assertEqual(body["status"], "skipped")
                self.assertIn("detail", body)

    def test_providers_separate_preset_verified_from_your_key(self):
        """`verified` 说的是「我们验证过这一行的 model id」，
        不是「你的 key 有效」—— 两件事共用一个标签的话，用户填完 key
        看到「未验证」会以为是自己填错了。
        """
        with tempfile.TemporaryDirectory() as td:
            client = self._app(ScriptedBackend({}), Path(td))
            with client:
                rows = client.get("/api/providers").json()
                row = next(r for r in rows if r["name"] == "deepseek")
                self.assertIn("preset_verified", row)
                self.assertIn("configured", row)
                # 预设验证过 ≠ 你填了 key，两个字段互不决定
                self.assertTrue(row["preset_verified"])

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


class TestBindGuard(unittest.TestCase):
    """绑定准入检查。**不带 fastapi 的 skip** —— `bind.py` 不依赖它，
    而这条防线恰恰是在依赖没装齐的机器上也必须成立的。
    """

    def test_loopback_forms_are_allowed(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.5.5.5", "LocalHost"):
            self.assertTrue(is_loopback_host(host), host)
            self.assertIsNone(check_bind_host(host), host)

    def test_exposed_hosts_are_refused_by_default(self):
        # 空串在 uvicorn 里等价于全接口，必须和 0.0.0.0 同等对待
        for host in ("0.0.0.0", "", "192.168.1.10", "::", "example.com"):
            self.assertFalse(is_loopback_host(host), host)
            refusal = check_bind_host(host)
            self.assertIsNotNone(refusal, host)
            self.assertIn("--i-know-its-exposed", refusal)

    def test_explicit_acknowledgement_allows_but_still_warns(self):
        self.assertIsNone(check_bind_host("0.0.0.0", acknowledged=True))
        self.assertIn("0.0.0.0", exposure_warning("0.0.0.0"))

    def test_unresolvable_name_is_not_treated_as_loopback(self):
        """解析不了就当暴露 —— 这里的默认必须偏保守。"""
        self.assertFalse(is_loopback_host("not a host"))
        self.assertIsNotNone(check_bind_host("not a host"))


if __name__ == "__main__":
    unittest.main()
