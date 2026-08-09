"""PROBE 模式（§3.2.1 / M3）。

PROBE 存在的理由是那个隐蔽失败模式：`GENERATIVE` 类任务几乎产生不了内容层硬信号，
于是「没有信号」既可能是一切正常，也可能是**伪装成健康的失败**。架构师无从区分，
所以只能定期主动看一眼。

这里钉四件事：
  1. 没有 PROBE 时，跑偏的 GENERATIVE 任务确实一路绿灯到底（PROBE 的存在理由）
  2. PROBE 到点会让出控制权，但**不是中断**——在轨就接着跑，不换 Subagent
  3. 判定跑偏时走的是既有中断链路，信号带 origin=architect_probe
  4. TRUST 任务一次探查都不做（PROBE 不能污染既有路径）
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, ToolCall
from cowork.agent.architect import AutoApproveGate
from cowork.llm import ArchitectVerdict
from cowork.llm.scripted import ScriptedBackend
from cowork.orchestrator import Orchestrator
from cowork.signals import SignalType, default_hard_signals
from cowork.store import SqliteStore
from cowork.types import (
    Criterion,
    SandboxProfile,
    SilencePolicy,
    TaskClass,
    TaskSpec,
    TaskStatus,
)

ON_TOPIC = "关于分布式系统一致性的说明：CAP 定理指出……"
OFF_TOPIC = "今天天气不错，我来讲个笑话。"


class ProbeFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-probe-"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def spec(self, *, generative=True, interval=0.001) -> TaskSpec:
        return TaskSpec(
            goal="写一篇关于分布式系统一致性的说明",
            parent_id="task_parent",  # 避开 §7.2 的顶层保护，让链路走完
            acceptance=[Criterion("c1", "内容切题且成结构")],  # 非机器可检
            task_class=TaskClass.GENERATIVE if generative else TaskClass.CODE,
            probe_interval_s=interval if generative else None,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=["draft.md"],
            max_steps=6,
        )

    def backend(self, contents: list[str], *, probe_for=None) -> ScriptedBackend:
        """每个 content 写一次 draft.md，最后 Finish。"""
        steps = {}
        for i, text in enumerate(contents):
            steps[(1, i)] = ToolCall("write_file", {"path": "draft.md", "content": text})
        steps[(1, len(contents))] = Finish(output={}, summary="写完了")
        return ScriptedBackend(
            steps,
            probe_for=probe_for,
            verdict_for=lambda spec, sigs: ArchitectVerdict(
                action="MODIFY_TASK",
                rationale="探查发现跑题，收紧约束",
                complexity_score=0.3,
                spec_changes={"added_criteria": [{"id": "c2", "description": "必须围绕 CAP"}]},
            ),
        )

    def orch(self, spec, backend) -> Orchestrator:
        return Orchestrator(
            spec, backend=backend, store=SqliteStore(),
            human_gate=AutoApproveGate(), log=lambda _m: None,
        )


class TestWhyProbeExists(ProbeFixture):
    def test_generative_has_no_content_layer_hard_signals(self):
        """§3.2.1：GENERATIVE 只剩资源类信号，内容跑偏产生不了任何硬信号。"""
        hard = default_hard_signals("GENERATIVE")
        for t in (SignalType.TEST_FAILED, SignalType.VALIDATION_FAILED,
                  SignalType.TOOL_FAILURE, SignalType.SCOPE_VIOLATION):
            self.assertNotIn(t, hard)
        self.assertIn(SignalType.STEP_LIMIT, hard)

    def test_generative_forces_probe_policy(self):
        """架构师无权把 GENERATIVE 设成 TRUST（§4.1 字段约束）。"""
        spec = self.spec()
        self.assertIs(spec.silence_policy, SilencePolicy.PROBE)

    def test_probe_interval_is_mandatory(self):
        with self.assertRaises(ValueError):
            TaskSpec(
                goal="x", acceptance=[Criterion("c1", "y")],
                task_class=TaskClass.GENERATIVE,
                sandbox=SandboxProfile(workspace=str(self.ws)),
            )


class TestProbeControlFlow(ProbeFixture):
    def test_on_track_probe_does_not_interrupt(self):
        """探查在轨 = 接着跑。不算中断、不换 Subagent、revision 不变。"""
        backend = self.backend([ON_TOPIC, ON_TOPIC + "补充"])
        orch = self.orch(self.spec(), backend)
        result = orch.run(max_cycles=3)

        self.assertIs(result.state.status, TaskStatus.COMPLETED)
        self.assertEqual(result.state.interrupt_count, 0)
        self.assertEqual(result.state.spec.revision, 1)
        self.assertGreater(orch.probe_count, 0, "间隔 1ms，至少该探查一次")
        self.assertEqual(backend.probe_calls, orch.probe_count)

    def test_probe_tokens_are_counted(self):
        """PROBE 是拿 token 换观测能力，成本必须进账（§3.2.1）。"""
        orch = self.orch(self.spec(), self.backend([ON_TOPIC]))
        result = orch.run(max_cycles=3)

        self.assertGreater(orch.probe_tokens, 0)
        self.assertGreaterEqual(result.state.tokens_used, orch.probe_tokens)

    def test_off_track_probe_goes_through_the_interrupt_chain(self):
        """判定跑偏 -> VALIDATION_FAILED(origin=architect_probe) -> 架构师决策。"""

        def probe_for(spec, ctx, excerpts):
            text = "".join(excerpts.values())
            return ("CAP" in text or "一致性" in text), "内容与目标无关"

        orch = self.orch(self.spec(), self.backend([OFF_TOPIC, ON_TOPIC], probe_for=probe_for))
        result = orch.run(max_cycles=3)

        sigs = orch.store.signals_for(result.state.spec.id)
        probe_sigs = [s for s in sigs if s.payload.get("origin") == "architect_probe"]
        self.assertTrue(probe_sigs, "跑偏没有变成信号")
        self.assertIs(probe_sigs[0].type, SignalType.VALIDATION_FAILED)
        self.assertGreaterEqual(result.state.interrupt_count, 1)
        self.assertTrue(result.decisions, "跑偏必须走到架构师决策")

    def test_excerpts_come_from_the_sandbox(self):
        """架构师看到的是产出的**内容**，不是文件名。读文件归 Runtime。"""
        seen: list[dict] = []

        def probe_for(spec, ctx, excerpts):
            seen.append(dict(excerpts))
            return True, "ok"

        orch = self.orch(self.spec(), self.backend([ON_TOPIC], probe_for=probe_for))
        orch.run(max_cycles=2)

        self.assertTrue(seen)
        self.assertEqual(list(seen[0]), ["draft.md"])
        self.assertIn("CAP", seen[0]["draft.md"])

    def test_step_budget_survives_probe_segments(self):
        """探查分段不能把 max_steps 清零。

        实测踩过：每段重新计数后 PROBE 任务变成没有步数上限 ——
        而 STEP_LIMIT 恰恰是 GENERATIVE 类仅剩的几条硬信号之一（§3.2.1），
        等于把它唯一的护栏拆了。
        """
        spec = self.spec()
        spec = spec.bump(revision=1, max_steps=3)
        # 脚本给 8 个 write_file，远超 max_steps=3
        backend = self.backend([ON_TOPIC] * 8, probe_for=lambda s, c, e: (True, "ok"))
        orch = self.orch(spec, backend)
        result = orch.run(max_cycles=1)

        sigs = [s.type for s in orch.store.signals_for(result.state.spec.id)]
        self.assertIn(SignalType.STEP_LIMIT, sigs, "步数上限没生效")
        self.assertLessEqual(result.state.current_step, 4)

    def test_no_progress_no_probe(self):
        """没有新 step 就不该再探查一次 —— 否则会空转烧 token。"""
        calls: list[int] = []

        def probe_for(spec, ctx, excerpts):
            calls.append(1)
            return True, "ok"

        # 只有一个 write_file + Finish：最多探查 1 次（第 2 个 step 边界之前）
        orch = self.orch(self.spec(), self.backend([ON_TOPIC], probe_for=probe_for))
        orch.run(max_cycles=2)

        self.assertLessEqual(len(calls), 2)


class TestTrustPathUnchanged(ProbeFixture):
    def test_trust_task_never_probes(self):
        """CODE / TRUST 任务一次探查都不做，行为与 M3 之前逐字相同。"""
        spec = self.spec(generative=False)
        self.assertIs(spec.silence_policy, SilencePolicy.TRUST)

        backend = self.backend([ON_TOPIC])
        orch = self.orch(spec, backend)
        orch.run(max_cycles=2)

        self.assertEqual(orch.probe_count, 0)
        self.assertEqual(backend.probe_calls, 0)


if __name__ == "__main__":
    unittest.main()
