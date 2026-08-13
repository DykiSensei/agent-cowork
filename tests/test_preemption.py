"""step 边界抢占：这是「外部中断」的全部实现（§10.1 / §5.1）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.agent.subagent import Subagent
from cowork.llm.scripted import ScriptedBackend
from cowork.runtime.bus import SignalBus
from cowork.runtime.loop import StepLoop
from cowork.runtime.sandbox import Sandbox
from cowork.signals import SignalType
from cowork.store import SqliteStore
from cowork.types import (
    AgentContext,
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskSpec,
    TaskStatus,
)


class LoopFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-test-"))
        (self.ws / "protected.txt").write_text("不该被改", encoding="utf-8")
        self.store = SqliteStore()
        self.bus = SignalBus()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def make(self, steps, *, scope=("out.py",), max_steps=6, acceptance=None,
             tools=None):
        spec = TaskSpec(
            goal="写文件",
            acceptance=acceptance or [Criterion("c1", "写出来就行")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=list(scope),
            max_steps=max_steps,
            # 默认给全套：这些用例大多在测工具本身，不测白名单
            tools=list(tools) if tools is not None else [
                "write_file", "read_file", "list_files", "search_files",
                "delete_file", "move_file", "run",
            ],
        )
        self.store.save_task(__import__("cowork").TaskState(spec=spec))
        sandbox = Sandbox(spec.sandbox, spec.scope)
        loop = StepLoop(bus=self.bus, sandbox=sandbox, store=self.store)
        agent = Subagent(ScriptedBackend(steps))
        return loop, agent, AgentContext(task_spec=spec)


class TestPreemption(LoopFixture):
    def test_human_intervention_preempts_before_first_step(self):
        """人的介入视同硬信号，最高优先级，立即抢占（§2.4）。"""
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("write_file", {"path": "out.py", "content": "x = 1"})}
        )
        self.bus.human_intervention(ctx.task_spec.id, "先停一下，方向要改")

        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertEqual(outcome.steps_run, 0, "抢占发生在派发下一个 step 之前")
        self.assertIs(outcome.preempting_signal.type, SignalType.HUMAN_INTERVENTION)
        self.assertFalse((self.ws / "out.py").exists(), "被抢占时不应有副作用")

    def test_human_intervention_mid_run(self):
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("write_file", {"path": "out.py", "content": "a"}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "b"}),
                (1, 2): Finish(output={}, summary="done"),
            }
        )

        original = agent.next_step

        def intercept(c):
            action, cost = original(c)
            # 第一个 step 执行完之后，人介入
            self.bus.human_intervention(c.task_spec.id, "停")
            agent.next_step = original
            return action, cost

        agent.next_step = intercept
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertEqual(outcome.steps_run, 1, "跑完当前 step 才停")
        self.assertIs(outcome.preempting_signal.type, SignalType.HUMAN_INTERVENTION)
        self.assertIsNotNone(outcome.checkpoint_id, "中断处必须有 checkpoint")


class TestHardSignals(LoopFixture):
    def test_scope_violation(self):
        """SCOPE_VIOLATION 兼作安全边界和跑偏探测器（§3.2 设计注记）。"""
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("write_file", {"path": "protected.txt", "content": "改了"})}
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)
        self.assertEqual(
            (self.ws / "protected.txt").read_text(encoding="utf-8"),
            "不该被改",
            "越界写入必须在落盘前被拦截",
        )

    def test_path_escape_is_scope_violation(self):
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("write_file", {"path": "../escaped.py", "content": "x"})}
        )
        outcome = loop.run(ctx, agent)
        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)

    def test_step_limit(self):
        never_finishes = {
            (1, i): ToolCall("write_file", {"path": "out.py", "content": str(i)})
            for i in range(20)
        }
        loop, agent, ctx = self.make(never_finishes, max_steps=3)
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertIs(outcome.preempting_signal.type, SignalType.STEP_LIMIT)
        self.assertEqual(outcome.steps_run, 3)

    def test_probe_read_of_missing_file_does_not_preempt(self):
        """探测一个还不存在的文件不是任务级失败（§11.6a）。

        M2 实测里这是最大的单一噪声源：Subagent 几乎总是先 read_file 探一下产出
        文件在不在，「不在」被当成 TOOL_FAILURE 抢占，每个任务白烧一轮架构师决策。
        失败结果照样进 reasoning_trace 回给模型，只是不产生硬信号。
        """
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("read_file", {"path": "out.py"}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
                (1, 2): Finish(output={}, summary="写完了"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        self.assertIsNone(outcome.preempting_signal)
        probe = next(e for e in ctx.reasoning_trace if e.get("name") == "read_file")
        self.assertFalse(probe["ok"], "失败事实仍要如实回给模型")

    def test_self_rehearsal_of_acceptance_command_does_not_preempt(self):
        """Subagent 自己预演验收命令失败，不该把架构师叫来（§11.6e）。

        验收的判定权归 Runtime 在 Finish 之后行使 —— 那一次失败仍然产生
        TEST_FAILED（见下一个测试），所以信号覆盖面一条没少，少的只是
        「架构师被叫来说一句继续」的那 3.5k token。
        """
        crit = Criterion("c1", "跑得过", ["python", "-c", "import sys; sys.exit(1)"])
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("run", {"command": list(crit.command)}),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
            },
            acceptance=[crit],
            max_steps=2,
        )
        outcome = loop.run(ctx, agent)

        # 两个 step 都跑完了才因 STEP_LIMIT 停 —— 说明第一步没抢占
        self.assertIs(outcome.preempting_signal.type, SignalType.STEP_LIMIT)
        rehearsal = ctx.reasoning_trace[1]
        self.assertEqual(rehearsal["name"], "run")
        self.assertFalse(rehearsal["ok"], "失败事实仍要如实回给模型")

    def test_acceptance_still_fails_at_finish(self):
        """预演不抢占，不等于验收失效。Finish 之后那次仍然产生 TEST_FAILED。"""
        crit = Criterion("c1", "跑得过", ["python", "-c", "import sys; sys.exit(1)"])
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("run", {"command": list(crit.command)}),
                (1, 1): Finish(output={}, summary="自认为好了"),
            },
            acceptance=[crit],
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.TEST_FAILED)

    def test_unrelated_command_failure_still_preempts(self):
        """只有验收命令本身豁免。别的命令炸了仍然是 TOOL_FAILURE。"""
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("run", {"command": ["python", "-c", "import sys; sys.exit(9)"]})},
            acceptance=[Criterion("c1", "跑得过", ["python", "verify.py"])],
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.TOOL_FAILURE)

    def test_list_files_replaces_the_ls_workaround(self):
        """没有 list_files 时真实 agent 只能去 run 一个 ls，然后越界（§11.6f）。"""
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall("list_files", {"path": "."}),
                (1, 1): Finish(output={}, summary="看过了"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        listing = ctx.reasoning_trace[1]
        self.assertTrue(listing["ok"])
        self.assertIn("protected.txt", listing["stdout"])

    def test_ls_is_still_a_scope_violation(self):
        """加了 list_files 不等于放开 allowed_binaries。"""
        loop, agent, ctx = self.make({(1, 0): ToolCall("run", {"command": ["ls"]})})
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)

    def test_tool_failure(self):
        loop, agent, ctx = self.make(
            {(1, 0): ToolCall("run", {"command": ["python", "-c", "import sys; sys.exit(3)"]})}
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.TOOL_FAILURE)
        self.assertEqual(outcome.preempting_signal.payload["exit_code"], 3)

    def test_validation_failed_on_bad_output(self):
        spec_steps = {(1, 0): Finish(output={"wrong": 1}, summary="乱填")}
        loop, agent, ctx = self.make(spec_steps)
        ctx.task_spec = ctx.task_spec.bump(
            revision=1,
            output_schema={
                "type": "object",
                "properties": {"file": {"type": "string"}},
                "required": ["file"],
            },
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.VALIDATION_FAILED)
        self.assertTrue(outcome.preempting_signal.payload["errors"])


class TestFilesystemErrorsStayInsideTheLoop(LoopFixture):
    """文件系统的拒绝是**工具失败**，不是我们的崩溃。

    这和 `run()` 固定 `encoding="utf-8", errors="replace"` 是同一条纪律，只是
    那次炸在 subprocess 的读取线程里、这次炸在 `read_text` 上：
    **工具层的失败必须以 ToolResult 的形式回到循环里**，那样架构师才有得判。
    抛出去的话，一个可以喂回给模型的错误会变成整个 run 的 traceback。
    """

    def test_reading_a_non_utf8_file_does_not_crash_the_loop(self):
        """上游任务写了个 GBK 文件 / 二进制产出，下游读一下就崩 —— 不行。"""
        (self.ws / "data.bin").write_bytes("中文".encode("gbk"))
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall(name="read_file", args={"path": "data.bin"},
                                 thought="看一眼上游产出"),
                (1, 1): Finish(output={}, summary="看完了"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        tool_records = [r for r in ctx.reasoning_trace if r.get("role") == "tool"]
        self.assertTrue(tool_records[0]["ok"], "读得到就算成功，坏字节替换掉即可")

    def test_write_to_a_directory_becomes_a_tool_result(self):
        """写到一个已经是目录的路径 —— 回 ToolResult，让模型自己换个路径。"""
        (self.ws / "out.py").mkdir()
        loop, agent, ctx = self.make(
            {
                (1, 0): ToolCall(name="write_file",
                                 args={"path": "out.py", "content": "x"},
                                 thought="写产出"),
            },
            max_steps=2,
        )
        outcome = loop.run(ctx, agent)

        # 硬失败 -> 中断交给架构师，但**不是异常**
        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        self.assertIs(outcome.preempting_signal.type, SignalType.TOOL_FAILURE)
        self.assertIn("写入失败", outcome.preempting_signal.raw_evidence)


class TestExpandedToolSurface(LoopFixture):
    """M10 加的四个工具。每一个都是在补一条「模型会绕路」的缺口（§11.6f 的模式）。"""

    def test_search_files_replaces_ten_read_files(self):
        """接手已有项目时最贵的那一步：定位代码。

        没有它只能 list_files + read_file 逐个试，而单个子任务默认 12 步。
        """
        (self.ws / "pkg").mkdir()
        (self.ws / "pkg" / "parser.py").write_text(
            "def parse_line(s):\n    return s.split(',')\n", encoding="utf-8")
        (self.ws / "pkg" / "cli.py").write_text("import parser\n", encoding="utf-8")
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="search_files", args={"pattern": r"def parse_line"},
                             thought="找它定义在哪"),
            (1, 1): Finish(output={}, summary="找到了"),
        })
        loop.run(ctx, agent)

        hit = [r for r in ctx.reasoning_trace if r.get("role") == "tool"][0]
        self.assertTrue(hit["ok"])
        self.assertIn("pkg/parser.py:1:", hit["stdout"])
        self.assertNotIn("cli.py", hit["stdout"])

    def test_search_skips_tooling_noise(self):
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "x.js").write_text("needle", encoding="utf-8")
        (self.ws / "real.txt").write_text("needle", encoding="utf-8")
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="search_files", args={"pattern": "needle"}, thought=""),
            (1, 1): Finish(output={}, summary=""),
        })
        loop.run(ctx, agent)
        out = [r for r in ctx.reasoning_trace if r.get("role") == "tool"][0]["stdout"]
        self.assertIn("real.txt", out)
        self.assertNotIn("node_modules", out)

    def test_search_miss_is_not_a_hard_failure(self):
        """搜不到是有效结果，不是故障 —— 同 read_file 探测不存在的文件。"""
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="search_files", args={"pattern": "找不到我"},
                             thought=""),
            (1, 1): Finish(output={}, summary=""),
        }, max_steps=4)
        outcome = loop.run(ctx, agent)
        self.assertIs(outcome.status, TaskStatus.COMPLETED, "不该因为搜不到就中断")

    def test_delete_is_bound_by_scope(self):
        """**这是 delete_file 存在的全部理由。**

        没有它，模型删东西只能 `run python -c "os.remove(...)"` —— 而 run 在本地
        沙箱里不受 scope 约束。缺一个受约束的删除，等于把删除推到唯一一条完全
        不受约束的路上。
        """
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="delete_file", args={"path": "protected.txt"},
                             thought="删掉它"),
        }, scope=("out.py",), max_steps=3)
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)
        self.assertTrue((self.ws / "protected.txt").exists(), "越界的删除不能真的发生")

    def test_delete_updates_the_produced_set(self):
        """删掉的文件不再是产出 —— 不同步的话验收和 PROBE 会去读一个不在的路径。"""
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="write_file", args={"path": "out.py", "content": "x"},
                             thought=""),
            (1, 1): ToolCall(name="delete_file", args={"path": "out.py"}, thought=""),
            (1, 2): Finish(output={}, summary=""),
        })
        loop.run(ctx, agent)
        self.assertEqual([a.content_ref for a in ctx.produced], [])

    def test_move_needs_both_ends_in_scope(self):
        """搬到 scope 外等于删，从 scope 外搬进来等于写 —— 两端都要判。"""
        (self.ws / "out.py").write_text("x", encoding="utf-8")
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="move_file",
                             args={"path": "out.py", "to": "escaped.py"}, thought=""),
        }, scope=("out.py",), max_steps=3)
        outcome = loop.run(ctx, agent)
        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)

    def test_move_rewrites_the_produced_path(self):
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="write_file", args={"path": "a.py", "content": "x"},
                             thought=""),
            (1, 1): ToolCall(name="move_file", args={"path": "a.py", "to": "b.py"},
                             thought=""),
            (1, 2): Finish(output={}, summary=""),
        }, scope=("a.py", "b.py"))
        loop.run(ctx, agent)
        self.assertEqual([a.content_ref for a in ctx.produced], ["b.py"])

    def test_recursive_listing_costs_one_step_instead_of_many(self):
        (self.ws / "a" / "b").mkdir(parents=True)
        (self.ws / "a" / "b" / "deep.txt").write_text("x", encoding="utf-8")
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="list_files", args={"path": ".", "recursive": True},
                             thought=""),
            (1, 1): Finish(output={}, summary=""),
        })
        loop.run(ctx, agent)
        out = [r for r in ctx.reasoning_trace if r.get("role") == "tool"][0]["stdout"]
        self.assertIn("a/b/deep.txt", out)

    def test_tools_is_a_real_allowlist_now(self):
        """`spec.tools` 以前只是声明：写上 ["read_file"] 照样能 run。

        声明和执行必须是同一份，否则前者是装饰（同 hard_signals 那条的反面 ——
        那个字段的语义是「预期」，这个字段的语义是「许可」）。
        """
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="run", args={"command": ["python", "-c", "pass"]},
                             thought=""),
        }, tools=("read_file",), max_steps=3)
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION)
        self.assertIn("tools", outcome.preempting_signal.payload["reason"])

    def test_fetch_url_refuses_non_http_schemes(self):
        """`file://` 能读任意本地文件 —— 那是绕开整个沙箱的一条路。"""
        from cowork.runtime.sandbox import Sandbox
        from cowork.types import SandboxProfile

        sb = Sandbox(SandboxProfile(workspace=str(self.ws)), ["out.py"])
        r = sb.fetch_url("file:///etc/passwd")
        self.assertFalse(r.ok)
        self.assertIn("只支持 http/https", r.stderr)

    def test_fetch_url_encodes_non_ascii_urls(self):
        """HTTP 头是 latin-1，中文域名直接发出去是 `UnicodeEncodeError` ——
        一个模型看不懂的错误，它会以为是网站的问题，再试一次还是它。

        断言只看「有没有编码」，不打网络：域名存不存在是另一回事。
        """
        from cowork.runtime.sandbox import Sandbox
        from cowork.types import SandboxProfile

        sb = Sandbox(SandboxProfile(workspace=str(self.ws)), ["out.py"])
        r = sb.fetch_url("https://例子.中国/路 径")
        self.assertFalse(r.ok)
        self.assertFalse(r.hard_failure, "取不到是软失败，不该抢占")
        self.assertNotIn("latin-1", r.stderr)
        self.assertIn("xn--", r.stderr, "域名要被 IDNA 编码后再发")

    def test_network_is_off_unless_the_task_says_so(self):
        """fetch_url 取回的是第三方文本，会进 trace 再进下一轮提示词。

        那是一条提示词注入通道，所以它必须由人显式打开，不能是默认。
        `search_web` 走同一条防线 —— 摘要同样是第三方文本。
        """
        from cowork.agent.architect import SpecTemplate
        from cowork.types import SandboxProfile

        default = SpecTemplate(sandbox=SandboxProfile(workspace=str(self.ws)))
        self.assertNotIn("fetch_url", default.tools)
        self.assertNotIn("search_web", default.tools)

        for call in (
            ToolCall(name="fetch_url", args={"url": "http://example.com"}, thought=""),
            ToolCall(name="search_web", args={"query": "怎么写"}, thought=""),
        ):
            loop, agent, ctx = self.make({(1, 0): call}, tools=("read_file",),
                                         max_steps=3)
            outcome = loop.run(ctx, agent)
            self.assertIs(outcome.preempting_signal.type, SignalType.SCOPE_VIOLATION,
                          f"{call.name} 不该在默认工具面里")


class TestBinaryApproval(LoopFixture):
    """撞 `run` 的程序白名单 → **交给人审批**，不是让人去改配置（M11）。

    这条路以前的终点是「人跑去设置页改 COWORK_ALLOWED_BINARIES 再重跑」——
    而那和「这一刻要不要放行它」根本不是同一个决定，中间还隔着一次重跑。
    现在信号带着程序名上来，人的裁决直接把它加进这个任务的白名单。
    """

    def test_signal_carries_the_binary_name(self):
        """按名字放行要有名字可用 —— **从 payload 取，不从理由文字里抠**。"""
        loop, agent, ctx = self.make({
            (1, 0): ToolCall(name="run", args={"command": ["curl", "http://x"]},
                             thought=""),
        }, tools=("run",), max_steps=3)
        outcome = loop.run(ctx, agent)

        sig = outcome.preempting_signal
        self.assertIs(sig.type, SignalType.SCOPE_VIOLATION)
        self.assertEqual(sig.payload.get("binary"), "curl")

    def test_message_does_not_send_the_model_to_change_settings(self):
        """这条消息模型也会看到。让它去改配置是误导 —— 它改不了。

        用 `curl`：它是**刻意不在**默认白名单里的那一类（对外、不可逆），
        所以这条断言不会因为默认名单以后放宽而失效。
        """
        from cowork.runtime.sandbox import Sandbox, ScopeViolation
        from cowork.types import SandboxProfile

        sb = Sandbox(SandboxProfile(workspace=str(self.ws)), ["out.py"])
        with self.assertRaises(ScopeViolation) as caught:
            sb.run(["curl", "https://example.com"])
        self.assertEqual(caught.exception.binary, "curl")
        self.assertNotIn("设置页", str(caught.exception))

    def test_granting_adds_it_to_this_task_only(self):
        """放行落在 spec 上（因此进 checkpoint、扛得住 restore），只对这个任务。"""
        from cowork.agent.architect import Architect, AutoApproveGate
        from cowork.policy import Policy
        from cowork.types import Criterion, SandboxProfile, TaskClass, TaskSpec

        spec = TaskSpec(
            goal="装依赖", acceptance=[Criterion("c1", "装上")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws)),
        )
        self.assertNotIn("curl", spec.sandbox.allowed_binaries)

        arch = Architect(
            backend=ScriptedBackend({}), store=self.store,
            human_gate=AutoApproveGate(), policy=Policy(),
        )
        new_spec = arch._apply_changes(spec, {"allow_binary": "curl"})

        self.assertIn("curl", new_spec.sandbox.allowed_binaries)
        self.assertEqual(new_spec.goal, spec.goal, "放行不该顺手改目标")
        # 原来那份一个字没动 —— 只对这个任务的这一版生效
        self.assertNotIn("curl", spec.sandbox.allowed_binaries)

    def test_granting_is_additive_only(self):
        """只能加、一次一个。交一整份列表 = 把「收窄白名单」也变成裁决能干的事。"""
        from cowork.agent.architect import Architect, AutoApproveGate
        from cowork.policy import Policy
        from cowork.types import Criterion, SandboxProfile, TaskClass, TaskSpec

        spec = TaskSpec(
            goal="x", acceptance=[Criterion("c1", "y")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(
                workspace=str(self.ws), allowed_binaries=("python", "node")
            ),
        )
        arch = Architect(
            backend=ScriptedBackend({}), store=self.store,
            human_gate=AutoApproveGate(), policy=Policy(),
        )
        out = arch._apply_changes(spec, {"allow_binary": "go"})
        self.assertEqual(out.sandbox.allowed_binaries, ("python", "node", "go"))

    def test_the_architect_cannot_grant_itself_a_binary(self):
        """**只有人能用这条。** 架构师自评的 schema 里没有这个字段 ——
        让被隔离方给自己配隔离边界是没有意义的（同 SpecTemplate 那条）。"""
        from cowork.llm.anthropic_backend import VERDICT_SCHEMA

        self.assertNotIn("allow_binary", VERDICT_SCHEMA["properties"])

    def test_modified_criteria_replaces_the_broken_command(self):
        """验收脚本语法错误时，架构师能用 modified_criteria 按 id 替换那条
        command —— 「多次改任务修不好」的根因就是 added_criteria 只能追加，
        原来那条错的 command 还在 acceptance 里，Runtime 每次都先跑它。"""
        from cowork.agent.architect import Architect, AutoApproveGate
        from cowork.policy import Policy
        from cowork.types import Criterion, SandboxProfile, TaskClass, TaskSpec

        spec = TaskSpec(
            goal="算一下",
            acceptance=[Criterion("c1", "跑得过", ["python", "-c", "import  sys"])],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws)),
        )
        arch = Architect(
            backend=ScriptedBackend({}), store=self.store,
            human_gate=AutoApproveGate(), policy=Policy(),
        )
        new_spec = arch._apply_changes(
            spec,
            {"modified_criteria": [
                {"id": "c1", "command": ["python", "-c", "import sys; sys.exit(0)"]}
            ]},
        )
        self.assertEqual(
            new_spec.acceptance[0].command,
            ["python", "-c", "import sys; sys.exit(0)"],
        )
        self.assertEqual(
            new_spec.acceptance[0].description, "跑得过",
            "没传 description 就不该动它",
        )
        # 原来那份一个字没动 —— 只对这个任务的这一版生效
        self.assertEqual(spec.acceptance[0].command, ["python", "-c", "import  sys"])

    def test_modified_criteria_empty_command_falls_back_to_manual(self):
        """command 给空数组 = 清空、退回人工判定（machine_checkable 变 False）。"""
        from cowork.agent.architect import Architect, AutoApproveGate
        from cowork.policy import Policy
        from cowork.types import Criterion, SandboxProfile, TaskClass, TaskSpec

        spec = TaskSpec(
            goal="x", acceptance=[Criterion("c1", "y", ["python", "-c", "pass"])],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws)),
        )
        arch = Architect(
            backend=ScriptedBackend({}), store=self.store,
            human_gate=AutoApproveGate(), policy=Policy(),
        )
        out = arch._apply_changes(spec, {"modified_criteria": [{"id": "c1", "command": []}]})
        self.assertIsNone(out.acceptance[0].command)
        self.assertFalse(out.acceptance[0].machine_checkable)


class TestAppendWrite(unittest.TestCase):
    """`write_file` 的 append 模式（M12 之后）：写长文件时一次输出有限，分多次追加。"""

    def _sandbox(self, ws: Path, scope=("out.py",)) -> Sandbox:
        return Sandbox(
            SandboxProfile(workspace=str(ws), allowed_binaries=("python",)),
            list(scope),
        )

    def test_append_adds_instead_of_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sandbox = self._sandbox(ws)
            self.assertTrue(sandbox.write_file("out.py", "第一段\n").ok)
            self.assertTrue(sandbox.write_file("out.py", "第二段\n", append=True).ok)
            self.assertEqual(
                (ws / "out.py").read_text(encoding="utf-8"), "第一段\n第二段\n"
            )

    def test_append_to_a_missing_file_creates_it(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sandbox = self._sandbox(ws)
            self.assertTrue(sandbox.write_file("out.py", "第一段\n", append=True).ok)
            self.assertEqual(
                (ws / "out.py").read_text(encoding="utf-8"), "第一段\n"
            )


class TestRunForcesUtf8(unittest.TestCase):
    """`run python` 强制 `-X utf8`：中文 Windows 上默认 GBK，会和 write_file/read_file
    的 UTF-8 打架 —— 文书任务（含中文文档）因此反复失败（M12 之后）。"""

    def test_run_python_writes_utf8_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sandbox = Sandbox(
                SandboxProfile(workspace=str(ws), allowed_binaries=("python",)),
                ["out.txt"],
            )
            # 不指定 encoding —— 依赖 -X utf8 强制成 UTF-8，而不是系统默认 GBK
            r = sandbox.run(["python", "-c", "open('out.txt','w').write('中文')"])
            self.assertTrue(r.ok, r.stderr)
            data = (ws / "out.txt").read_bytes()
            self.assertEqual(data.decode("utf-8"), "中文")

    def test_run_python_reads_utf8_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sandbox = Sandbox(
                SandboxProfile(workspace=str(ws), allowed_binaries=("python",)),
                ["out.txt"],
            )
            (ws / "out.txt").write_text("中文", encoding="utf-8")
            # 不指定 encoding 读 UTF-8 文件，应该原样读到「中文」
            r = sandbox.run(
                ["python", "-c",
                 "print(open('out.txt').read() == '中文')"]
            )
            self.assertTrue(r.ok, r.stderr)
            self.assertIn("True", r.stdout)


class TestToolFaceIsConsistent(unittest.TestCase):
    """加一个工具要同时改四处，少一处就是一类**假信号**。

    §11.6f 那条实测：缺一个列目录工具，M2 的 75 次运行里 23 次假
    SCOPE_VIOLATION —— 模型绕路、撞白名单、白烧一轮架构师决策。反过来也一样：
    schema 里有而提示词里没有，模型不知道能用；提示词里有而派发里没有，
    调了直接「未声明的工具」。这四处以前没有任何测试钉着，全靠人记得。
    """

    def _tools(self) -> list[str]:
        from cowork.llm.anthropic_backend import ACTION_SCHEMA

        return [t for t in ACTION_SCHEMA["properties"]["tool"]["enum"] if t]

    def test_every_tool_in_the_schema_has_a_sandbox_method(self):
        from cowork.runtime.sandbox import Sandbox

        for tool in self._tools():
            self.assertTrue(callable(getattr(Sandbox, tool, None)),
                            f"schema 里有 {tool}，Sandbox 上却没有")

    def test_every_tool_in_the_schema_is_described_to_the_model(self):
        from cowork.llm.anthropic_backend import SUBAGENT_SYSTEM

        for tool in self._tools():
            self.assertIn(f"tool={tool}", SUBAGENT_SYSTEM,
                          f"{tool} 没写进 Subagent 提示词 —— 模型不会知道它存在")

    def test_every_tool_in_the_schema_is_dispatched_by_the_loop(self):
        """派发缺一个的话，模型照着提示词调它，得到的是「未声明的工具」。"""
        from unittest import mock

        from cowork.runtime.loop import StepLoop
        from cowork.runtime.sandbox import ScopeViolation

        loop = StepLoop.__new__(StepLoop)
        loop.sandbox = mock.MagicMock()
        # 每个键都给上：`_exec_tool` 直接下标取参数，缺哪个都是 KeyError
        args = {"path": "p", "content": "c", "pattern": "x", "glob": "**/*",
                "to": "t", "url": "http://e.com", "query": "q",
                "command": ["python"], "recursive": False}
        spec = mock.MagicMock()
        spec.tools = ()  # 空 = 不过滤，测的是派发不是白名单

        for tool in self._tools():
            try:
                loop._exec_tool(ToolCall(name=tool, args=dict(args), thought=""), spec)
            except ScopeViolation as exc:  # pragma: no cover - 失败路径
                self.fail(f"{tool} 在 _exec_tool 里没有派发分支: {exc}")


class TestAcceptanceCommandWhitelist(unittest.TestCase):
    """验收 command 的程序必须落在 run 白名单里。`test`/`sh`/`[` 这类 shell 命令
    不在白名单，模型拆解生成 command 时条件反射写 `test -f`、决策替换时又换成
    `sh -c` 变体 —— 三处提示词都得把这条说清楚，否则「没有 test / 没有 sh」会
    一直失败（真人反馈）。"""

    def test_decompose_states_shell_commands_are_off_whitelist(self):
        from cowork.llm.anthropic_backend import DECOMPOSE_SYSTEM

        self.assertIn("不在白名单", DECOMPOSE_SYSTEM)
        self.assertIn("test", DECOMPOSE_SYSTEM)
        self.assertIn("sh", DECOMPOSE_SYSTEM)

    def test_architect_replacement_states_shell_variants_wont_help(self):
        from cowork.llm.anthropic_backend import ARCHITECT_SYSTEM

        self.assertIn("shell 变体", ARCHITECT_SYSTEM)
        self.assertIn("allowed_binaries", ARCHITECT_SYSTEM)

    def test_environment_renders_the_same_constraint(self):
        from cowork.agent.architect import Architect, AutoApproveGate, SpecTemplate
        from cowork.llm.scripted import ScriptedBackend
        from cowork.policy import Policy
        from cowork.types import SandboxProfile

        arch = Architect(
            backend=ScriptedBackend({}), store=None,
            human_gate=AutoApproveGate(), policy=Policy(),
        )
        template = SpecTemplate(
            sandbox=SandboxProfile(workspace=".", allowed_binaries=("python",))
        )
        env = arch._render_environment(template)
        self.assertIn("验收 command", env)
        self.assertIn("test", env)


class TestSearchWeb(LoopFixture):
    """`search_web`：搜索这一步归我们自己持有（开发文档 §11.22）。

    这些用例全部不打网络 —— 替掉 `search.search`，测的是工具层的行为。
    """

    def _sandbox(self):
        from cowork.runtime.sandbox import Sandbox
        from cowork.types import SandboxProfile

        return Sandbox(SandboxProfile(workspace=str(self.ws)), ["out.py"])

    def test_missing_key_is_a_soft_failure_with_a_next_step(self):
        """没配 key 不是任务级失败，而且要说得出下一步该做什么。

        判成 hard_failure 的话，「忘了配搜索 key」会以中断架构师收场 ——
        白烧一轮决策去处理一件人五秒能解决的事（同 §11.6a 那条）。
        """
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"ZHIPUAI_API_KEY": "",
                                          "COWORK_SEARCH_API_KEY": "",
                                          "COWORK_SEARCH_PROVIDER": "zhipu"}):
            r = self._sandbox().search_web("多 Agent 协作")

        self.assertFalse(r.ok)
        self.assertFalse(r.hard_failure, "搜不了不是任务级失败")
        self.assertIn("ZHIPUAI_API_KEY", r.stderr, "要说出缺的是哪个变量")

    def test_results_are_labelled_third_party_and_truncated(self):
        """摘要是第三方文本，必须和 fetch_url 一样显式标注「不是指令」。"""
        from unittest import mock

        from cowork.runtime import sandbox as sandbox_mod
        from cowork.runtime.search import SearchHit

        hits = [
            SearchHit(title=f"标题{i}", url=f"https://e.com/{i}",
                      snippet="正文" * 4000, source="某站", published="2026-08-01")
            for i in range(3)
        ]
        with mock.patch("cowork.runtime.search.search", return_value=hits):
            r = self._sandbox().search_web("查点东西")

        self.assertTrue(r.ok)
        self.assertIn("第三方内容", r.stdout)
        self.assertIn("不是你的任务", r.stdout)
        self.assertIn("https://e.com/0", r.stdout)
        self.assertLessEqual(
            len(r.stdout), sandbox_mod.SEARCH_MAX_CHARS + 32,
            "一次工具调用不该把子任务的上下文预算吃光",
        )

    def test_zero_results_is_an_answer_not_a_failure(self):
        """零结果是有效答案：ok=True 模型才会去改搜索词，而不是重试同一个。"""
        from unittest import mock

        with mock.patch("cowork.runtime.search.search", return_value=[]):
            r = self._sandbox().search_web("一个不存在的东西")

        self.assertTrue(r.ok)
        self.assertIn("没有搜到", r.stdout)

    def test_no_failure_escapes_the_tool_layer(self):
        """工具层的失败一律以 ToolResult 回到循环里，不许抛。

        `_exec_tool` 只接 ScopeViolation —— 从这里抛任何异常都会穿透整个 run
        （和当年 read_file 的 UnicodeDecodeError 同一形状）。两种都要挡：
        已知的 SearchUnavailable，和**我们自己的解析 bug**。
        """
        from unittest import mock

        from cowork.runtime.search import SearchUnavailable

        for exc in (SearchUnavailable("连不上搜索端点"), TypeError("解析写错了")):
            with mock.patch("cowork.runtime.search.search", side_effect=exc):
                r = self._sandbox().search_web("x")
            self.assertFalse(r.ok, f"{type(exc).__name__} 要变成失败的 ToolResult")
            self.assertFalse(r.hard_failure, "搜不了不该抢占架构师")

    def test_the_key_never_reaches_the_model_or_the_record(self):
        """key 不进三个地方之一：这里是「不进模型上下文」。"""
        import os
        from unittest import mock

        from cowork.runtime.search import SearchHit

        secret = "sk-zhipu-should-never-appear-1234567890"
        with mock.patch.dict(os.environ, {"ZHIPUAI_API_KEY": secret}):
            with mock.patch("cowork.runtime.search.search",
                            return_value=[SearchHit(title="t", url="https://e.com")]):
                r = self._sandbox().search_web("x")
        self.assertNotIn(secret, r.stdout + r.stderr + r.detail)

    def test_tool_is_dispatched_when_the_task_allows_it(self):
        """白名单放行时，query 要真的走到沙箱那一层（三处改动的接缝）。"""
        from unittest import mock

        from cowork.runtime.search import SearchHit

        with mock.patch("cowork.runtime.search.search",
                        return_value=[SearchHit(title="命中", url="https://e.com")]) as m:
            loop, agent, ctx = self.make({
                (1, 0): ToolCall(name="search_web", args={"query": "协作系统"},
                                 thought=""),
                (1, 1): Finish(output={}, summary="done"),
            }, tools=("search_web",), max_steps=4)
            outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED)
        self.assertEqual(m.call_args.args[0], "协作系统")


def _search_configured() -> str | None:
    from cowork.runtime import search as search_api

    return search_api.configured()


@unittest.skipUnless(_search_configured(), "未配搜索 key（ZHIPUAI_API_KEY）")
class TestLiveSearch(unittest.TestCase):
    """打真实搜索端点。

    **这条在没有 key 时是 skip，不是通过** —— `search.py` 的请求体和响应字段
    映射是照文档写的，从未在真实端点上跑过。这个项目里 `PROVIDERS.verified`
    记的就是这个区别：没打通过不等于错，等于**没验证**，两者不能混。
    配上 key 之后这条用例就是那次验证。
    """

    def test_a_real_query_comes_back_with_usable_hits(self):
        from cowork.runtime.search import search

        hits = search("多 Agent 协作系统", count=3)
        self.assertTrue(hits, "真实端点返回了空列表 —— 请求体或字段映射对不上")
        first = hits[0]
        self.assertTrue(first.url.startswith("http"), f"link 字段没对上: {first!r}")
        self.assertTrue(first.title, "title 字段没对上")


class TestSoftSignalsDoNotPreempt(LoopFixture):
    def test_soft_signal_is_queued_not_preempting(self):
        from cowork.actions import SoftSignalAction

        loop, agent, ctx = self.make(
            {
                (1, 0): SoftSignalAction("AMBIGUITY", "goal 里没说要不要处理空串"),
                (1, 1): ToolCall("write_file", {"path": "out.py", "content": "x = 1"}),
                (1, 2): Finish(output={}, summary="done"),
            }
        )
        outcome = loop.run(ctx, agent)

        self.assertIs(outcome.status, TaskStatus.COMPLETED, "软信号无权要求中断")
        self.assertEqual(len(outcome.soft_signals), 1)
        self.assertEqual(outcome.soft_signals[0].level.value, "L1")
        self.assertEqual(outcome.soft_signals[0].source.value, "SUBAGENT")


if __name__ == "__main__":
    unittest.main()
