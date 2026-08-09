"""M1.4 端到端：代理侧预算拒绝 -> Runtime 产出 BUDGET_EXCEEDED 硬信号。

这是 §7.2「成本失控即升级」那条规则真正的落地验证：
应用层的 TaskSpec.token_budget 是软限制（自己数自己的，agent 骗自己就失效），
LiteLLM virtual key 是硬限制（代理侧拒绝，绕不过去）。
这组测试证明后者能变成架构师可消费的信号，而不是让整个 run 崩掉。

不需要有效的 ANTHROPIC_API_KEY —— 预算检查在转发上游之前。
前置：docker compose up -d litellm
"""

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cowork.agent.architect import AutoApproveGate
from cowork.llm.errors import BudgetExceeded, ModelError
from cowork.orchestrator import Orchestrator
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
    TaskState,
    TaskStatus,
)

PROXY = "http://localhost:4000"
MASTER_KEY = "sk-cowork-master"


def _admin(path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{PROXY}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MASTER_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _proxy_available() -> bool:
    try:
        with urllib.request.urlopen(f"{PROXY}/health/liveliness", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


class ExhaustedBudgetBackend:
    """一个用真实 SDK、指向真实代理、但 key 已超预算的后端。"""

    name = "anthropic-exhausted"

    def __init__(self, key: str):
        from cowork.llm.anthropic_backend import AnthropicBackend

        # max_retries=0：预算拒绝重试永远不会成功，测试里不必等
        self.inner = AnthropicBackend(base_url=PROXY, api_key=key, max_retries=0)

    def __getattr__(self, item):
        return getattr(self.inner, item)


@unittest.skipUnless(_proxy_available(), f"LiteLLM 代理不可达 ({PROXY})")
class TestBudgetEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        status, body = _admin(
            "/key/generate",
            {
                "models": ["claude-opus-5", "claude-haiku-4-5"],
                "max_budget": 0.01,
                "key_alias": "cowork-e2e-budget",
            },
        )
        assert status == 200, f"建 key 失败: {status} {body}"
        cls.key = json.loads(body)["key"]
        _admin("/key/update", {"key": cls.key, "spend": 99.0})

    @classmethod
    def tearDownClass(cls):
        _admin("/key/delete", {"keys": [cls.key]})

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-budget-"))
        self.store = SqliteStore()
        self.spec = TaskSpec(
            goal="随便写点什么",
            acceptance=[Criterion("c1", "有产出")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=["out.py"],
            model="claude-haiku-4-5",
            max_steps=3,
            token_budget=10_000_000,   # 应用层软限制故意放得极大
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_backend_raises_budget_exceeded(self):
        backend = ExhaustedBudgetBackend(self.key)
        with self.assertRaises(BudgetExceeded) as cm:
            backend.next_step(AgentContext(task_spec=self.spec))
        self.assertIs(cm.exception.signal_type, SignalType.BUDGET_EXCEEDED)
        self.assertEqual(cm.exception.status_code, 429)

    def test_loop_turns_it_into_a_hard_signal(self):
        """关键断言：应用层预算还剩 1000 万 token，但代理拒了 -> 照样中断。"""
        self.store.save_task(TaskState(spec=self.spec))
        loop = StepLoop(
            bus=SignalBus(),
            sandbox=Sandbox(self.spec.sandbox, self.spec.scope),
            store=self.store,
        )
        outcome = loop.run(AgentContext(task_spec=self.spec), ExhaustedBudgetBackend(self.key))

        self.assertIs(outcome.status, TaskStatus.INTERRUPTED)
        sig = outcome.preempting_signal
        self.assertIs(sig.type, SignalType.BUDGET_EXCEEDED)
        self.assertEqual(sig.level.value, "L0")
        self.assertEqual(sig.source.value, "RUNTIME")
        self.assertEqual(sig.payload["origin"], "model_call")
        self.assertEqual(sig.payload["status_code"], 429)
        self.assertIn("Budget has been exceeded", sig.raw_evidence)
        self.assertIsNotNone(outcome.checkpoint_id, "中断处仍要落 checkpoint")

    def test_orchestrator_suspends_when_architect_also_broke(self):
        """Subagent 和架构师共用一把 key 时，预算耗尽会同时打掉两者。

        没有决策者就不能瞎猜 —— 应该挂起等人，而不是崩、也不是自作主张继续。
        """
        orch = Orchestrator(
            self.spec,
            backend=ExhaustedBudgetBackend(self.key),
            store=self.store,
            human_gate=AutoApproveGate(),
            log=lambda _m: None,
        )
        result = orch.run(max_cycles=2)

        self.assertIs(result.state.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(result.state.interrupt_count, 1)
        sigs = self.store.signals_for(self.spec.id)
        self.assertTrue(
            any(s.type is SignalType.BUDGET_EXCEEDED for s in sigs),
            "预算硬信号必须落库，人接手时要看得到",
        )


if __name__ == "__main__":
    unittest.main()
