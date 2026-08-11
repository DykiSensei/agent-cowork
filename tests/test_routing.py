"""按任务路由到不同供应商（§10.3.3）。

三条边界是这一层的全部意义，破了就不是这个架构了：

  只有 Subagent 被路由    架构师永远只有一个（§2.3）
  模型不选模型            分配由人定，架构师只描述任务特点
  分配落在 spec 上        进存储、进界面、进 checkpoint，不只活在内存里
"""

from __future__ import annotations

import tempfile
import unittest

from cowork.agent.architect import Architect, AutoApproveGate
from cowork.llm.routing import RoutingBackend, assign_providers, split_model
from cowork.llm.scripted import ScriptedBackend
from cowork.policy import Policy
from cowork.store import SqliteStore
from cowork.types import AgentContext, Criterion, SandboxProfile, TaskClass, TaskSpec


def spec(tid="t1", *, model="claude-opus-5", goal="干活") -> TaskSpec:
    return TaskSpec(
        id=tid, goal=goal, acceptance=[Criterion("c1", "做完")],
        task_class=TaskClass.CODE, model=model,
        sandbox=SandboxProfile(workspace=tempfile.mkdtemp()),
    )


class TestModelString(unittest.TestCase):
    def test_prefix_is_split(self):
        self.assertEqual(split_model("kimi:kimi-k3"), ("kimi", "kimi-k3"))

    def test_bare_model_stays_bare(self):
        """老 spec、手写 spec 都不带前缀 —— 不能因此报错或路由错。"""
        self.assertEqual(split_model("claude-opus-5"), (None, "claude-opus-5"))

    def test_malformed_prefix_is_treated_as_bare(self):
        for bad in (":x", "kimi:", ":"):
            self.assertIsNone(split_model(bad)[0], bad)


class TestAssignProviders(unittest.TestCase):
    def setUp(self):
        self.specs = [spec("t1"), spec("t2"), spec("t3")]
        self.models = {"kimi": "kimi-k3", "deepseek": "deepseek-v4-flash"}

    def test_writes_provider_into_the_spec(self):
        out = assign_providers(self.specs, {"t1": "kimi"}, self.models)
        self.assertEqual(out[0].model, "kimi:kimi-k3")

    def test_untouched_tasks_keep_their_model(self):
        """人只挑了一部分，剩下的保持默认 —— 不替他补。"""
        out = assign_providers(self.specs, {"t1": "kimi"}, self.models)
        self.assertEqual(out[1].model, "claude-opus-5")

    def test_revision_is_not_bumped(self):
        """这发生在任何一次派发之前，不是「改任务」。

        加一次 revision 会让后面每条 DecisionRecord 的 revision 都错位一格。
        """
        out = assign_providers(self.specs, {"t1": "kimi"}, self.models)
        self.assertEqual(out[0].revision, self.specs[0].revision)

    def test_assignment_survives_serialization(self):
        """要能进存储和界面，不能只活在内存里。"""
        out = assign_providers(self.specs, {"t1": "deepseek"}, self.models)
        back = TaskSpec.from_dict(out[0].to_dict())
        self.assertEqual(split_model(back.model), ("deepseek", "deepseek-v4-flash"))


class TestRoutingBackend(unittest.TestCase):
    def setUp(self):
        self.default = ScriptedBackend({}, decompose_for=lambda g, f: [])
        self.kimi = ScriptedBackend({}, decompose_for=lambda g, f: [])
        self.deepseek = ScriptedBackend({}, decompose_for=lambda g, f: [])
        self.routing = RoutingBackend(
            self.default, {"kimi": self.kimi, "deepseek": self.deepseek})

    def _step(self, model):
        self.routing.next_step(AgentContext(task_spec=spec(model=model)))

    def test_routes_by_prefix(self):
        self._step("kimi:kimi-k3")
        self._step("deepseek:deepseek-v4-flash")
        self.assertEqual(self.routing.used, {"kimi": 1, "deepseek": 1})

    def test_unknown_prefix_falls_back_visibly(self):
        """派发时因为一个模型名把整条链打挂不划算 —— 回落，但要记下来。"""
        self._step("没这家:某模型")
        self._step("claude-opus-5")
        self.assertEqual(self.routing.used, {"default": 2})

    def test_only_next_step_is_routed(self):
        """架构师永远只有一个（§2.3）—— 其余方法全部走 default。"""
        target = spec()
        ctx = AgentContext(task_spec=target)

        self.routing.review_decomposition("目标", [target])
        self.routing.decompose("目标")
        self.routing.summarize(ctx)
        self.routing.verify(target, ctx)
        self.routing.probe(target, ctx, {})
        self.routing.profile_tasks([target])

        self.assertEqual(self.default.review_calls, 1)
        self.assertEqual(self.default.decompose_calls, 1)
        self.assertEqual(self.default.probe_calls, 1)
        self.assertEqual(self.default.profile_calls, 1)
        self.assertEqual(self.kimi.review_calls, 0)
        self.assertEqual(self.kimi.decompose_calls, 0)
        self.assertEqual(self.routing.used, {}, "这些都不该被路由")

    def test_cache_stats_are_merged(self):
        """跨供应商的代价之一是缓存各自冷启动 —— 合起来看才是真实成本。"""
        from cowork.llm import CacheStats

        for b, (prompt, cached) in ((self.default, (100, 50)), (self.kimi, (200, 0))):
            b.cache_stats = CacheStats(calls=1, calls_with_usage=1,
                                       prompt_tokens=prompt, cached_tokens=cached)
        merged = self.routing.cache_stats
        self.assertEqual(merged.prompt_tokens, 300)
        self.assertEqual(merged.cached_tokens, 50)


class TestArchitectAssignsModels(unittest.TestCase):
    """一次架构师调用（描述） + 一次人的决定（选择）。"""

    def _architect(self, gate):
        return Architect(ScriptedBackend({}), SqliteStore(), policy=Policy(),
                         human_gate=gate)

    def test_single_provider_skips_the_question_entirely(self):
        """没得选的时候提问是在浪费人的注意力，而且白花一次调用。"""
        arch = self._architect(_PickAll("kimi"))
        specs, profiles = arch.assign_models([spec("t1")], {"kimi": "kimi-k3"})

        self.assertEqual(specs[0].model, "claude-opus-5", "没换")
        self.assertEqual(profiles, [])
        self.assertEqual(arch.backend.profile_calls, 0, "不该花那次描述调用")

    def test_gate_without_the_method_means_no_choosing(self):
        class OnlyInterrupts:
            def review(self, *a):
                raise AssertionError("不该走中断网关")

        arch = self._architect(OnlyInterrupts())
        specs, _ = arch.assign_models(
            [spec("t1")], {"kimi": "kimi-k3", "deepseek": "deepseek-v4-flash"})
        self.assertEqual(specs[0].model, "claude-opus-5")
        self.assertEqual(arch.backend.profile_calls, 0)

    def test_profiles_then_assigns(self):
        arch = self._architect(_PickAll("kimi"))
        providers = {"kimi": "kimi-k3", "deepseek": "deepseek-v4-flash"}
        specs, profiles = arch.assign_models([spec("t1"), spec("t2")], providers)

        self.assertEqual(arch.backend.profile_calls, 1, "一次调用，不是每任务一次")
        self.assertEqual(len(profiles), 2)
        self.assertEqual([s.model for s in specs], ["kimi:kimi-k3"] * 2)

    def test_unknown_provider_from_the_human_is_ignored(self):
        arch = self._architect(_PickAll("不存在的家"))
        providers = {"kimi": "kimi-k3", "deepseek": "deepseek-v4-flash"}
        specs, _ = arch.assign_models([spec("t1")], providers)
        self.assertEqual(specs[0].model, "claude-opus-5", "回落默认，不写坏 spec")

    def test_auto_approve_gate_does_not_choose_for_you(self):
        """让「自动」去挑供应商 = 把人的决定伪装成系统的决定。"""
        arch = self._architect(AutoApproveGate())
        providers = {"kimi": "kimi-k3", "deepseek": "deepseek-v4-flash"}
        specs, _ = arch.assign_models([spec("t1")], providers)
        self.assertEqual(specs[0].model, "claude-opus-5")

    def test_tokens_are_accounted(self):
        arch = self._architect(_PickAll("kimi"))
        before = arch.tokens_used
        arch.assign_models([spec("t1")], {"kimi": "k", "deepseek": "d"})
        self.assertGreater(arch.tokens_used, before)


class _PickAll:
    """把所有任务都指给同一家的假网关。"""

    def __init__(self, provider: str):
        self.provider = provider

    def review(self, *a):
        raise AssertionError("不该走中断网关")

    def assign_models(self, profiles, providers):
        return {p.task_id: self.provider for p in profiles}


class TestAvailableProviders(unittest.TestCase):
    """默认就是「用户填了哪家的 api 就用哪家」。"""

    def setUp(self):
        import os

        self._saved = {k: os.environ.get(k) for k in
                       ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY",
                        "GEMINI_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY",
                        "XAI_API_KEY", "ARK_API_KEY", "ANTHROPIC_API_KEY",
                        "COWORK_LLM_API_KEY")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        import os

        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_only_providers_with_keys(self):
        import os

        from cowork.cli import available_providers

        self.assertEqual(available_providers(), {})
        os.environ["MOONSHOT_API_KEY"] = "sk-x"
        self.assertEqual(available_providers(), {"kimi": "kimi-k3"})

    def test_litellm_never_counts(self):
        """代理的模型由代理侧决定，给不出「这家的 Subagent 模型」。"""
        import os

        from cowork.cli import available_providers

        os.environ["COWORK_LLM_API_KEY"] = "sk-virtual"
        self.assertNotIn("litellm", available_providers())


class TestWrappersMatchTheProtocol(unittest.TestCase):
    """**包一层 Backend 的类，签名必须跟着协议走。**

    实测炸过：M10 给 `decompose` 加了 `existing`（接手已有项目要的工作区现状），
    真实后端都改了，而 `RoutingBackend` / `BudgetedBackend` 这两个**包装类**
    各自重新声明了一遍签名、没人跟着改 —— 于是只要配了按角色选供应商或开着
    token 护栏（默认就开），拆解 100% 挂在
    `TypeError: decompose() got an unexpected keyword argument 'existing'`。

    单测全绿是因为它们都直接拿真实/脚本后端测，**没有一条测试是隔着包装层发起的**
    （同 §11.20 那条：契约写了什么，就要有一条从调用方那侧发起的测试）。

    这条用例比逐个修更值钱：它把「加一个参数要改几处」变成机器来记。
    """

    WRAPPERS = ("routing.RoutingBackend", "budget.BudgetedBackend")

    def _wrapper_classes(self):
        import importlib

        for path in self.WRAPPERS:
            mod, _, cls = path.partition(".")
            yield path, getattr(importlib.import_module(f"cowork.llm.{mod}"), cls)

    def test_no_wrapper_drops_a_protocol_parameter(self):
        import inspect

        from cowork.llm import Backend

        for path, wrapper in self._wrapper_classes():
            for name in dir(Backend):
                if name.startswith("_"):
                    continue
                proto = getattr(Backend, name, None)
                impl = getattr(wrapper, name, None)
                if not callable(proto) or not callable(impl):
                    continue
                try:
                    want = set(inspect.signature(proto).parameters) - {"self"}
                    got = set(inspect.signature(impl).parameters) - {"self"}
                except (TypeError, ValueError):  # pragma: no cover
                    continue
                if "kwargs" in got:
                    continue  # 原样透传的不用逐个对
                self.assertEqual(
                    want - got, set(),
                    f"{path}.{name} 少了协议里的参数 —— 调用方一传就是 TypeError",
                )

    def test_decompose_survives_both_wrappers(self):
        """真的隔着两层包装调一次 —— 上面那条是形状，这条是行为。"""
        from cowork.llm.budget import BudgetedBackend, CostGuard

        inner = ScriptedBackend({}, decompose_for=lambda goal, feedback: [])
        routed = RoutingBackend(inner, {})
        wrapped = BudgetedBackend(routed, CostGuard(0))

        for backend in (routed, wrapped):
            drafts, tokens = backend.decompose(
                "把 CSV 转成周报", feedback=["缺一条验收标准"], existing="README.md\n",
            )
            self.assertIsInstance(drafts, list)
            self.assertIsInstance(tokens, int)

        # 现状真的穿过了两层包装 —— 只对签名不够，值丢了照样是「当成空目录重建」
        self.assertEqual(inner.decompose_existing, ["README.md\n"] * 2)


if __name__ == "__main__":
    unittest.main()
