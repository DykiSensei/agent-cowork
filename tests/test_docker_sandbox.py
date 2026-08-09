"""M1.2：Docker 沙箱的越权拦截必须发生在容器边界，而不只是工具层白名单。

连不上 Docker 就 skip。需要 `docker pull python:3.12-slim`。
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.agent.subagent import Subagent
from cowork.llm.scripted import ScriptedBackend
from cowork.runtime.bus import SignalBus
from cowork.runtime.loop import StepLoop
from cowork.runtime.sandbox import Sandbox, ScopeViolation
from cowork.signals import SignalType
from cowork.store import SqliteStore
from cowork.types import (
    AgentContext,
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskState,
    TaskStatus,
)

# 相对路径，两种模式下都指向 workspace 里的 verify.py（容器 cwd 是 /w）
TAMPER = "open('verify.py','a').write('# tampered')"


def _docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except Exception:
        return False


class DockerFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-docker-"))
        # verify.py 在 scope 之外：Subagent 不该能改它来让测试通过
        (self.ws / "verify.py").write_text("print('ok')\n", encoding="utf-8")
        (self.ws / "solution.py").write_text("x = 1\n", encoding="utf-8")
        self.store = SqliteStore()
        self.bus = SignalBus()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def make(self, steps, *, use_docker: bool):
        spec = TaskSpec(
            goal="改 solution.py",
            acceptance=[Criterion("c1", "跑得动")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(
                workspace=str(self.ws),
                allowed_binaries=("python",),
                use_docker=use_docker,
            ),
            scope=["solution.py"],
            max_steps=4,
        )
        self.store.save_task(TaskState(spec=spec))
        loop = StepLoop(
            bus=self.bus,
            sandbox=Sandbox(spec.sandbox, spec.scope),
            store=self.store,
        )
        return loop, Subagent(ScriptedBackend(steps)), AgentContext(task_spec=spec)


@unittest.skipUnless(_docker_available(), "Docker 引擎不可达")
class TestDockerContainment(DockerFixture):
    def test_out_of_scope_write_from_run_is_scope_violation(self):
        """出口标准：越权访问真实触发 SCOPE_VIOLATION。

        这次不是工具层拦的 —— write_file 的 scope 检查根本没参与，
        是容器把 workspace 只读挂载，内核层面拒绝了写入。
        """
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("run", {"command": ["python", "-c", TAMPER]})},
            use_docker=True,
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)
        self.assertIn("只读挂载", outcome.preempting_signal.payload["reason"])
        self.assertEqual(
            (self.ws / "verify.py").read_text(encoding="utf-8"),
            "print('ok')\n",
            "scope 外文件必须原样",
        )

    def test_in_scope_write_from_run_succeeds(self):
        """反面：scope 内的路径在容器里必须可写，否则隔离就成了误伤。"""
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall(
                    "run",
                    {"command": ["python", "-c", "open('/w/solution.py','w').write('y = 2\\n')"]},
                ),
                (1, 1): Finish(output={}, summary="done"),
            },
            use_docker=True,
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        self.assertEqual((self.ws / "solution.py").read_text(encoding="utf-8"), "y = 2\n")

    def test_new_in_scope_file_is_mountable(self):
        """scope 里声明但尚不存在的文件，容器内也应可写（先建空文件再挂载）。"""
        (self.ws / "solution.py").unlink()
        sandbox = Sandbox(
            SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",), use_docker=True),
            ["solution.py"],
        )
        res = sandbox.run(["python", "-c", "open('/w/solution.py','w').write('z = 3\\n')"])
        self.assertTrue(res.ok, res.stderr)
        self.assertEqual((self.ws / "solution.py").read_text(encoding="utf-8"), "z = 3\n")

    def test_network_is_disabled(self):
        sandbox = Sandbox(
            SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",), use_docker=True),
            ["solution.py"],
        )
        res = sandbox.run(
            ["python", "-c", "import socket; socket.create_connection(('1.1.1.1',53),timeout=3)"]
        )
        self.assertFalse(res.ok, "沙箱不应有出网能力")


@unittest.skipUnless(_docker_available(), "Docker 引擎不可达")
class TestWhyTheContainerIsLoadBearing(DockerFixture):
    def test_local_sandbox_does_not_contain_run(self):
        """记录本地沙箱的真实边界：工具层白名单管不住 `run`。

        这条不是 bug 报告，是 M1.2 存在的理由 —— 本地模式下 run 能改 scope 外文件，
        所以「只靠工具层白名单」不满足出口标准，容器隔离是必需的而非锦上添花。
        """
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("run", {"command": ["python", "-c", TAMPER]})},
            use_docker=False,
        )
        loop.run(ctx, agent)

        self.assertIn(
            "tampered",
            (self.ws / "verify.py").read_text(encoding="utf-8"),
            "本地模式下 run 确实能越界 —— 这正是需要容器边界的原因",
        )

    def test_tool_layer_still_blocks_write_file(self):
        """对照：走 write_file 工具时，工具层白名单照常拦截。"""
        sandbox = Sandbox(
            SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            ["solution.py"],
        )
        with self.assertRaises(ScopeViolation):
            sandbox.write_file("verify.py", "# tampered")


if __name__ == "__main__":
    unittest.main()
