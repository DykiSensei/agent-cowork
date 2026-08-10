"""OpenAI 兼容后端（DeepSeek / Kimi / 任何 OpenAI 兼容端点）。

前半段不需要任何 key：验证 JSON 解析与本地校验这条兜底链路 ——
它存在的理由是不赌供应商的结构化输出支持。

后半段打真实供应商，只在设了对应 key 时才跑：
    DEEPSEEK_API_KEY=sk-... python -m unittest tests.test_openai_compat
"""

import json
import os
import shutil
import unittest

from cowork.llm.anthropic_backend import _parse_action
from cowork.llm.errors import ModelError
from cowork.llm.openai_compat import _parse_and_validate

ACTION_LIKE = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "finish"]},
        "thought": {"type": "string"},
    },
    "required": ["kind", "thought"],
}


class TestJsonSalvage(unittest.TestCase):
    """供应商的 JSON 模式各家脾气不同，本地这层兜底必须自己站得住。"""

    def test_plain_json(self):
        data, errs = _parse_and_validate('{"kind":"finish","thought":"done"}', ACTION_LIKE)
        self.assertEqual(errs, [])
        self.assertEqual(data["kind"], "finish")

    def test_markdown_fence_is_stripped(self):
        raw = '```json\n{"kind":"finish","thought":"done"}\n```'
        data, errs = _parse_and_validate(raw, ACTION_LIKE)
        self.assertEqual(errs, [])
        self.assertEqual(data["kind"], "finish")

    def test_bare_fence_is_stripped(self):
        data, _ = _parse_and_validate('```\n{"kind":"finish","thought":"x"}\n```', ACTION_LIKE)
        self.assertIsNotNone(data)

    def test_invalid_json_reports_error(self):
        data, errs = _parse_and_validate("这不是 JSON", ACTION_LIKE)
        self.assertIsNone(data)
        self.assertIn("不是合法 JSON", errs[0])

    def test_schema_violation_is_caught_locally(self):
        """供应商即使不支持 schema 约束，也不该让脏数据流进 Subagent。"""
        data, errs = _parse_and_validate('{"kind":"nope","thought":"x"}', ACTION_LIKE)
        self.assertIsNone(data)
        self.assertTrue(any("enum" in e for e in errs), errs)

    def test_missing_required_field(self):
        data, errs = _parse_and_validate('{"kind":"finish"}', ACTION_LIKE)
        self.assertIsNone(data)
        self.assertTrue(any("缺少必填字段" in e for e in errs), errs)

    def test_toplevel_array_rejected(self):
        data, errs = _parse_and_validate('[{"kind":"finish"}]', ACTION_LIKE)
        self.assertIsNone(data)
        self.assertIn("顶层必须是对象", errs[0])


class TestActionParsing(unittest.TestCase):
    """schema 通过 ≠ 语义有效。

    ACTION_SCHEMA 用空串表示「本字段不适用」，于是 kind=tool_call + tool="" 是
    合法 JSON、能过本地校验，再往下才炸。M2 跑批里 75 次运行有 3 次死在这里
    （§11.6b）。解析失败必须是 ModelError —— 那样 step 循环会把它变成硬信号交给
    架构师，而不是让异常穿透整个 run。
    """

    def _full(self, **over):
        base = {
            "kind": "finish", "thought": "", "tool": "", "path": "", "content": "",
            "command": [], "output_json": "", "summary": "", "signal_type": "", "detail": "",
        }
        base.update(over)
        return base

    def test_empty_tool_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="tool_call", tool=""))

    def test_unknown_tool_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="tool_call", tool="rm_rf"))

    def test_empty_signal_type_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="soft_signal", signal_type=""))

    def test_hard_signal_type_rejected_as_soft(self):
        """软信号通道不能用来伪造硬信号 —— 那是 Runtime 的专属职责（§3.1）。"""
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="soft_signal", signal_type="TEST_FAILED"))

    def test_unknown_kind_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="rm -rf /"))

    def test_valid_actions_still_parse(self):
        a = _parse_action(self._full(kind="tool_call", tool="write_file",
                                     path="x.py", content="y"))
        self.assertEqual((a.name, a.args["path"]), ("write_file", "x.py"))
        b = _parse_action(self._full(kind="soft_signal", signal_type="AMBIGUITY",
                                     detail="不确定"))
        self.assertEqual(b.signal_type, "AMBIGUITY")
        c = _parse_action(self._full(kind="finish", output_json='{"a":1}', summary="s"))
        self.assertEqual(c.output, {"a": 1})


class TestProviderResolution(unittest.TestCase):
    """回归：两家 key 同时存在时，各家必须拿到自己的 base_url 和 key。

    实测踩过——`--backend kimi` 曾把 DeepSeek 的 key 发到 LiteLLM 代理上，
    因为 base_url 的预设查表漏了 kimi，而 key 走的是后端内部的固定顺序回退链。
    单独设一家 key 时这个 bug 不会显形。
    """

    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
                      "COWORK_LLM_BASE_URL", "COWORK_LLM_API_KEY")
        }
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek-fake"
        os.environ["MOONSHOT_API_KEY"] = "sk-moonshot-fake"
        os.environ.pop("COWORK_LLM_BASE_URL", None)
        os.environ.pop("COWORK_LLM_API_KEY", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_each_provider_gets_its_own_endpoint_and_key(self):
        from cowork.cli import _make_backend

        ds = _make_backend("deepseek")
        self.assertIn("deepseek.com", str(ds.client.base_url))
        self.assertEqual(ds.client.api_key, "sk-deepseek-fake")

        km = _make_backend("kimi")
        self.assertIn("moonshot.cn", str(km.client.base_url))
        self.assertEqual(km.client.api_key, "sk-moonshot-fake")

    def test_env_override_wins(self):
        from cowork.cli import _make_backend

        os.environ["COWORK_LLM_BASE_URL"] = "http://localhost:4000/v1"
        os.environ["COWORK_LLM_API_KEY"] = "sk-virtual-key"
        b = _make_backend("deepseek")
        self.assertIn("localhost:4000", str(b.client.base_url))
        self.assertEqual(b.client.api_key, "sk-virtual-key")

    def test_deepseek_preset_uses_a_currently_served_model(self):
        """DeepSeek v4 起只暴露 flash / pro 两个 id，chat / reasoner 只剩别名。

        这条钉的是「预设里写的是当前真的存在的 model id」，不是分工 ——
        三个角色现在统一 flash，§4.1 的「架构师用推理档」要靠
        COWORK_ARCHITECT_MODEL=deepseek-v4-pro 显式拿回来。
        """
        from cowork.cli import _make_backend

        ds = _make_backend("deepseek")
        self.assertEqual(ds.architect_model, "deepseek-v4-flash")
        self.assertEqual(ds.subagent_model, "deepseek-v4-flash")

    def test_architect_model_can_be_lifted_back_to_the_reasoning_tier(self):
        from cowork.cli import _make_backend

        os.environ["COWORK_ARCHITECT_MODEL"] = "deepseek-v4-pro"
        self.addCleanup(os.environ.pop, "COWORK_ARCHITECT_MODEL", None)
        self.assertEqual(_make_backend("deepseek").architect_model, "deepseek-v4-pro")


class TestReviewerResolution(unittest.TestCase):
    """--reviewer auto 的意图只有一条：复核者尽量不是拆解者自己（§11.11）。"""

    def test_auto_picks_the_default_reviewer_for_real_backends(self):
        from cowork.cli import DEFAULT_REVIEWER, resolve_reviewer

        self.assertEqual(resolve_reviewer("deepseek", "auto"), DEFAULT_REVIEWER)

    def test_auto_swaps_direction_when_the_generator_is_already_the_reviewer(self):
        from cowork.cli import DEFAULT_REVIEWER, resolve_reviewer

        self.assertEqual(resolve_reviewer(DEFAULT_REVIEWER, "auto"), "deepseek")

    def test_scripted_backend_does_not_pay_for_a_review(self):
        from cowork.cli import resolve_reviewer

        self.assertIsNone(resolve_reviewer("scripted", "auto"))

    def test_none_falls_back_to_same_model_review(self):
        from cowork.cli import resolve_reviewer

        self.assertIsNone(resolve_reviewer("deepseek", "none"))

    def test_explicit_choice_wins(self):
        from cowork.cli import resolve_reviewer

        self.assertEqual(resolve_reviewer("deepseek", "anthropic"), "anthropic")


class TestRepairRound(unittest.TestCase):
    """回归：空回复不能被原样回灌进修复轮。

    OpenAI 兼容端点拒绝 content 为空的 assistant 消息（400 "must not be empty"），
    于是修复轮的**请求本身**非法 —— 一次可恢复的解析失败被升级成硬失败，
    重试机会白白吃掉。M7 7.2 的 120 次复核调用里有 2 次栽在这（§11.11）。
    """

    def _backend(self, replies):
        from cowork.llm.openai_compat import OpenAICompatBackend

        b = OpenAICompatBackend(base_url="http://localhost:1/v1", api_key="sk-fake")
        b.client = _FakeClient(replies)
        return b

    def test_empty_reply_does_not_produce_an_empty_assistant_turn(self):
        b = self._backend(["", '{"sufficient": true, "missing": []}'])
        sufficient, missing, _ = b.review_decomposition("目标", [])

        self.assertTrue(sufficient)
        self.assertEqual(missing, [])
        second = b.client.calls[1]["messages"]
        self.assertTrue(
            all(m["content"].strip() for m in second),
            f"修复轮里出现了空消息：{second}",
        )

    def test_truncated_reply_is_retried_clean_not_repaired(self):
        """截断是掷骰子（thinking 吃掉了额度），把残文回灌只会让它接着写半截 JSON。"""
        b = self._backend([])
        b.client = _FakeClient(
            ['{"sufficient": tr', '{"sufficient": true, "missing": []}'],
            finish_reasons=["length", "stop"],
        )
        sufficient, _, _ = b.review_decomposition("目标", [])

        self.assertTrue(sufficient)
        self.assertEqual(
            b.client.calls[1]["messages"], b.client.calls[0]["messages"],
            "截断重试应原样重发，不带残文",
        )

    def test_truncation_says_so_instead_of_blaming_the_json(self):
        b = self._backend([])
        b.client = _FakeClient(["", ""], finish_reasons=["length", "length"])
        with self.assertRaises(ModelError) as caught:
            b.review_decomposition("目标", [])
        self.assertIn("截断", str(caught.exception))

    def test_non_empty_bad_reply_is_still_echoed_back(self):
        """有内容的坏输出要原样回灌 —— 模型得看见自己写错了什么。"""
        b = self._backend(["这不是 JSON", '{"sufficient": false, "missing": ["x"]}'])
        sufficient, missing, _ = b.review_decomposition("目标", [])

        self.assertFalse(sufficient)
        self.assertEqual(missing, ["x"])
        roles = [m["role"] for m in b.client.calls[1]["messages"]]
        self.assertIn("assistant", roles)


class _FakeClient:
    """按脚本依次返回 content 的假 client，记录每轮请求。"""

    def __init__(self, replies, finish_reasons=None):
        self.replies = list(replies)
        self.finish_reasons = list(finish_reasons or [])
        self.calls = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0) if self.replies else ""
        reason = self.finish_reasons.pop(0) if self.finish_reasons else "stop"
        msg = type("M", (), {"content": text})()
        choice = type("C", (), {"message": msg, "finish_reason": reason})()
        return type("R", (), {"choices": [choice], "usage": None})()


class TestBackendWiring(unittest.TestCase):
    def test_reasoner_skips_json_mode(self):
        """deepseek-reasoner 不支持 response_format，只能靠提示词约束。"""
        from cowork.llm.openai_compat import _NO_JSON_MODE

        self.assertTrue(any(m in "deepseek-reasoner" for m in _NO_JSON_MODE))

    def test_presets(self):
        from cowork.llm.openai_compat import PRESETS

        self.assertIn("deepseek", PRESETS)
        self.assertIn("moonshot", PRESETS)


def _live_provider() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)，没配 key 就返回 None。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return ("https://api.deepseek.com/v1", os.environ["DEEPSEEK_API_KEY"], "deepseek-chat")
    if os.environ.get("MOONSHOT_API_KEY"):
        return ("https://api.moonshot.cn/v1", os.environ["MOONSHOT_API_KEY"], "kimi-k3")
    return None


@unittest.skipUnless(_live_provider(), "未设 DEEPSEEK_API_KEY / MOONSHOT_API_KEY")
class TestLiveProvider(unittest.TestCase):
    """打真实供应商。验证的是「结构化输出这条路走不走得通」，不是模型聪不聪明。"""

    @classmethod
    def setUpClass(cls):
        from cowork.llm.openai_compat import OpenAICompatBackend

        base, key, model = _live_provider()
        cls.model = model
        cls.backend = OpenAICompatBackend(
            base_url=base, api_key=key, subagent_model=model,
            architect_model=model, triage_model=model,
        )

    def test_structured_output_roundtrip(self):
        data, tokens = self.backend._call(
            model=self.model,
            system="你是一个测试桩。",
            user="返回 kind=finish，thought 写「测试」。",
            schema=ACTION_LIKE,
        )
        self.assertEqual(data["kind"], "finish")
        self.assertGreater(tokens, 0, "usage 必须能读出来，否则预算统计是假的")

    def test_full_chain_with_real_model(self):
        """M1.3 出口：demo 场景用真实模型跑通。

        断言只到「终局状态合法且有账可查」为止。原来这里要求必须
        COMPLETED 或 AWAITING_HUMAN，M2 实测证明那是错的：同一 spec 同一模型
        跨 75 次运行中断次数落在 0–5，跑满 max_cycles 后 FAILED 是**设计内的**
        终局，不是缺陷（§11.6d）。拿概率性结果做断言只会得到一个偶发红灯的测试。
        """
        from cowork import demo
        from cowork.types import TaskStatus

        orch, ws = demo.build(backend=self.backend)
        orch.log = lambda _m: None
        try:
            result = orch.run(max_cycles=4)
            self.assertIn(
                result.state.status,
                (TaskStatus.COMPLETED, TaskStatus.AWAITING_HUMAN,
                 TaskStatus.FAILED, TaskStatus.ABANDONED),
                "终局状态必须是状态机里定义过的那几个",
            )
            self.assertGreater(result.state.tokens_used, 0)
            if result.state.status is not TaskStatus.COMPLETED:
                # 没成不要紧，但必须留下为什么没成的记录 —— 这是 §7.3 可见性的底线
                self.assertTrue(
                    result.decisions,
                    "非 COMPLETED 的终局必须有 DecisionRecord 解释原因",
                )
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
